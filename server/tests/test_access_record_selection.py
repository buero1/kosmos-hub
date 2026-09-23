import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app.api.routes import accounts
from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.hub_access_control import HubTeam, HubRecordAssignment, HubRecordAccessGrant
from app.services.hub_operations import HubOperationService, HubOperationError, agent_operations
from test_hub_finance_operations import env, req


def payload(env, **extra):
    return {"module_key": "customers", "record_ids": json.dumps([str(env.customer.id), str(env.hidden.id)]), **extra}


def test_selection_query_named_paged_searchable_and_readonly(env):
    env.hidden.is_visible = True
    env.db.add_all([Customer(name=f"Customer {index:03d}") for index in range(103)])
    env.db.commit()
    first = env.service.query("access.records.options", {"module_key": "customers"})
    assert first["total"] == 105 and len(first["items"]) == 100 and first["next_offset"] == "100"
    second = env.service.query("access.records.options", {"module_key": "customers", "offset": "100"})
    assert len(second["items"]) == 5 and not second["next_offset"]
    assert len({row["record_id"] for row in first["items"] + second["items"]}) == 105
    filtered = env.service.query("access.records.options", {"module_key": "customers", "query": "FINANCE", "limit": "5000"})
    assert filtered["total"] == 1 and filtered["items"][0]["name"] == env.customer.name
    assert not env.db.new and not env.db.dirty


def test_selection_shows_existing_owner_and_team(env):
    team = HubTeam(name="Sales")
    env.db.add(team)
    env.db.flush()
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=team.id)
    env.db.commit()
    record = env.service.query("access.records.options", {"module_key": "customers", "query": "Finance"})["items"][0]
    assert record["owner"] == "sales" and record["team"] == "Sales"
    assert "encrypted_profile_json" not in record


def test_lead_selection_and_bulk_assignment(env):
    lead = HubLead(encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {"Company": "Picker Lead", "Last_Name": "Person"}})))
    env.db.add(lead)
    env.db.commit()
    data = env.service.query("access.records.options", {"module_key": "leads"})
    assert data["total"] == 1 and data["items"][0]["record_id"] == str(lead.id)
    result = env.service.execute("access.records.assign_many", {"module_key": "leads", "record_ids": json.dumps([str(lead.id)]), "owner_user_id": str(env.sales.id)})
    assert result.outputs["changed"] == "1"
    assert env.access.can_access_record(user=env.sales, module_key="leads", record_id=lead.id)


def test_bulk_assignment_preserves_unmentioned_owner_and_explicit_clear(env):
    team = HubTeam(name="Test team")
    env.db.add(team)
    env.db.flush()
    for customer in (env.customer, env.hidden):
        env.access.assign_record(module_key="customers", record_id=customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.commit()
    result = env.service.execute("access.records.assign_many", payload(env, team_id=str(team.id)))
    assert result.outputs["changed"] == "2"
    rows = env.db.scalars(select(HubRecordAssignment)).all()
    assert all(row.team_id == team.id and row.owner_user_id == env.sales.id for row in rows)
    env.service.execute("access.records.assign_many", payload(env, owner_user_id=""))
    assert all(row.team_id == team.id and row.owner_user_id is None for row in rows)
    env.sales.team_id = team.id
    env.sales.role = "management"
    env.db.flush()
    assert all(env.access.can_access_record(user=env.sales, module_key="customers", record_id=c.id, action="edit") for c in (env.customer, env.hidden))


def test_grants_to_user_and_team_are_idempotent_and_do_not_replace_assignment(env):
    team = HubTeam(name="Readers")
    env.db.add(team)
    env.db.flush()
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.user.id, team_id=None)
    env.db.commit()
    for _ in range(2):
        env.service.execute("access.grants.create_many", payload(env, user_id=str(env.sales.id)))
    assert env.db.scalar(select(func.count()).select_from(HubRecordAccessGrant)) == 2
    assert env.access.can_access_record(user=env.sales, module_key="customers", record_id=env.customer.id)
    assert not env.access.can_access_record(user=env.sales, module_key="customers", record_id=env.customer.id, action="edit")
    env.service.execute("access.grants.create_many", payload(env, team_id=str(team.id), can_edit="true"))
    assert env.db.scalar(select(HubRecordAssignment)).owner_user_id == env.user.id


@pytest.mark.parametrize("extra", [{"record_ids": "[]"}, {"record_ids": '[true]'}, {"record_ids": '[1]'},
    {"record_ids": '["0"]'}, {"record_ids": '{"all":true}'}, {"record_ids": '*'}, {"record_ids": '["1", "999999"]'},
    {"module_key": "sites"}, {"owner_user_id": "invalid"}, {"owner_user_id": "9999"}, {"password": "x"},
    {"record_ids": json.dumps(["1"] * 5001)}])
def test_invalid_selection_is_atomic(env, extra):
    with pytest.raises(ValueError):
        env.service.execute("access.records.assign_many", payload(env, owner_user_id=str(env.sales.id)) | extra)
    assert env.db.scalar(select(func.count()).select_from(HubRecordAssignment)) == 0


def test_failure_halfway_rolls_back_all_changes(env, monkeypatch):
    original = env.access.__class__.assign_record
    calls = []
    def fail_second(self, **values):
        calls.append(values)
        if len(calls) == 2:
            raise ValueError("Test failure")
        return original(self, **values)
    monkeypatch.setattr(env.access.__class__, "assign_record", fail_second)
    with pytest.raises(ValueError):
        env.service.execute("access.records.assign_many", payload(env, team_id="", owner_user_id=str(env.sales.id)))
    assert env.db.scalar(select(func.count()).select_from(HubRecordAssignment)) == 0


def test_no_privilege_escalation_or_inactive_targets(env):
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.query("access.records.options", {"module_key": "customers"})
    for key in ("access.records.assign_many", "access.grants.create_many"):
        with pytest.raises(HubOperationError):
            limited.execute(key, payload(env, team_id=""))
    env.sales.is_active = False
    env.db.flush()
    with pytest.raises(HubOperationError):
        env.service.execute("access.records.assign_many", payload(env, owner_user_id=str(env.sales.id)))
    env.user.is_active = False
    env.db.flush()
    with pytest.raises(HubOperationError):
        env.service.query("access.records.options", {"module_key": "customers"})


@pytest.mark.parametrize("data", [{}, {"user_id": "1", "team_id": "1"}, {"user_id": "1", "can_edit": "yes"}])
def test_grants_require_exactly_one_target(env, data):
    with pytest.raises(ValueError):
        env.service.execute("access.grants.create_many", payload(env, **data))
    assert env.db.scalar(select(func.count()).select_from(HubRecordAccessGrant)) == 0


def test_http_and_agent_share_batch_operation_and_error_preserves_data(env, monkeypatch):
    monkeypatch.setattr(accounts, "_require_admin_user", lambda request: env.user)
    monkeypatch.setattr(accounts, "require_csrf", lambda *args: None)
    assert {"access.records.assign_many", "access.grants.create_many"} <= {op.key for op in agent_operations()}
    data = {**payload(env), "kind": "assign", "owner_user_id": str(env.sales.id), "team_id": "__keep__"}
    response = asyncio.run(accounts.save_record_access_batch(req(env, data), env.db))
    assert response.status_code == 200 and json.loads(response.body)["changed"] == "2"
    data["record_ids"] = '["99999"]'
    response = asyncio.run(accounts.save_record_access_batch(req(env, data), env.db))
    assert response.status_code == 400 and "detail" in json.loads(response.body)
    assert env.db.scalar(select(func.count()).select_from(HubRecordAssignment)) == 2


def test_csrf_failure_prevents_write(env, monkeypatch):
    monkeypatch.setattr(accounts, "_require_admin_user", lambda request: env.user)
    request = req(env, {**payload(env), "kind": "assign"})
    request.session = {"csrf_token": "expected"}
    with pytest.raises(HTTPException) as error:
        asyncio.run(accounts.save_record_access_batch(request, env.db))
    assert error.value.status_code == 403
    assert env.db.scalar(select(func.count()).select_from(HubRecordAssignment)) == 0


def team_setup(env):
    team, other = HubTeam(name="Team A"), HubTeam(name="Team B")
    env.db.add_all([team, other])
    env.db.flush()
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=team.id)
    env.access.assign_record(module_key="customers", record_id=env.hidden.id, owner_user_id=env.user.id, team_id=other.id)
    env.access.add_grant(module_key="customers", record_id=env.customer.id, user_id=env.sales.id, team_id=None, can_edit=True)
    env.db.commit()
    return team, other


def team_values(env, team, selected):
    data = env.service.query("access.records.options", {"module_key": "customers", "team_id": str(team.id)})
    return {"module_key": "customers", "team_id": str(team.id), "record_ids": json.dumps(selected),
            "previous_record_ids": json.dumps(data["previous_record_ids"])}


def test_team_snapshot_is_complete_even_when_search_or_page_hides_selection(env):
    team, _ = team_setup(env)
    values = {"module_key": "customers", "team_id": str(team.id), "query": "no-match", "offset": "100", "limit": "1"}
    data = env.service.query("access.records.options", values)
    assert data["items"] == [] and data["previous_record_ids"] == [str(env.customer.id)]
    assert data["selected_items"][0]["name"] == env.customer.name
    assert data["selected_items"][0]["team"] == team.name
    env.customer.is_visible = False
    env.db.commit()
    data = env.service.query("access.records.options", values | {"query": "", "offset": "0", "limit": "100"})
    assert str(env.customer.id) in {item["record_id"] for item in data["items"]}
    assert data["selected_items"][0]["name"] == env.customer.name
    assert not env.db.new and not env.db.dirty


def test_team_selection_add_remove_reload_preserves_owners_grants_other_teams(env):
    team, other = team_setup(env)
    new = Customer(name="New team customer")
    env.db.add(new)
    env.db.commit()
    values = team_values(env, team, [str(new.id)])
    result = env.service.execute("access.records.assign_many", values)
    env.db.commit()
    env.db.expire_all()
    rows = {row.record_id: row for row in env.db.scalars(select(HubRecordAssignment))}
    assert rows[env.customer.id].team_id is None and rows[env.customer.id].owner_user_id == env.sales.id
    assert rows[new.id].team_id == team.id
    assert rows[env.hidden.id].team_id == other.id and rows[env.hidden.id].owner_user_id == env.user.id
    assert env.db.scalar(select(HubRecordAccessGrant)).can_edit
    assert result.outputs["changed"] == "2"
    fresh = env.service.query("access.records.options", {"module_key": "customers", "team_id": str(team.id)})
    assert fresh["previous_record_ids"] == [str(new.id)]


def test_empty_selection_removes_only_loaded_team_not_user_or_explicit_grants(env):
    team, other = team_setup(env)
    env.service.execute("access.records.assign_many", team_values(env, team, []))
    rows = {row.record_id: row for row in env.db.scalars(select(HubRecordAssignment))}
    assert rows[env.customer.id].team_id is None and rows[env.customer.id].owner_user_id == env.sales.id
    assert rows[env.hidden.id].team_id == other.id
    assert env.db.scalar(select(func.count()).select_from(HubRecordAccessGrant)) == 1
    assert team_values(env, team, [])["previous_record_ids"] == "[]"


@pytest.mark.parametrize("change", ["added", "moved", "removed"])
def test_stale_team_snapshot_fails_without_overwriting_concurrent_change(env, change):
    team, other = team_setup(env)
    values = team_values(env, team, [])
    if change == "added":
        env.access.assign_record(module_key="customers", record_id=env.hidden.id, owner_user_id=env.user.id, team_id=team.id)
    else:
        env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id,
                                 team_id=other.id if change == "moved" else None)
    env.db.commit()
    before = [(row.record_id, row.team_id, row.owner_user_id) for row in env.db.scalars(select(HubRecordAssignment))]
    with pytest.raises(HubOperationError, match="inzwischen"):
        env.service.execute("access.records.assign_many", values)
    assert before == [(row.record_id, row.team_id, row.owner_user_id) for row in env.db.scalars(select(HubRecordAssignment))]


@pytest.mark.parametrize("extra", [{"team_id": ""}, {"team_id": "9999"}, {"previous_record_ids": "*"},
                                  {"previous_record_ids": "[true]"}, {"previous_record_ids": "[]"}, {"record_ids": '["99999"]'}])
def test_invalid_team_reconciliation_does_not_clear_assignments(env, extra):
    team, _ = team_setup(env)
    with pytest.raises(ValueError):
        env.service.execute("access.records.assign_many", team_values(env, team, []) | extra)
    assert env.db.scalar(select(HubRecordAssignment).where(HubRecordAssignment.record_id == env.customer.id)).team_id == team.id


def test_team_reconciliation_rolls_back_deselection_if_addition_fails(env, monkeypatch):
    team, other = team_setup(env)
    values = team_values(env, team, [str(env.hidden.id)])
    original = env.access.__class__.assign_record
    def fail_add(self, **kwargs):
        if kwargs["team_id"] is not None:
            raise ValueError("Failed addition")
        return original(self, **kwargs)
    monkeypatch.setattr(env.access.__class__, "assign_record", fail_add)
    with pytest.raises(ValueError, match="Failed addition"):
        env.service.execute("access.records.assign_many", values)
    rows = {row.record_id: row for row in env.db.scalars(select(HubRecordAssignment))}
    assert rows[env.customer.id].team_id == team.id and rows[env.hidden.id].team_id == other.id


def test_team_selection_http_roundtrip_and_admin_boundary(env, monkeypatch):
    team, _ = team_setup(env)
    monkeypatch.setattr(accounts, "_require_admin_user", lambda request: env.user)
    monkeypatch.setattr(accounts, "require_csrf", lambda *args: None)
    response = accounts.access_record_options(req(env, {}), env.db, team_id=str(team.id))
    snapshot = json.loads(response.body)["previous_record_ids"]
    response = asyncio.run(accounts.save_record_access_batch(req(env, {"kind": "assign", "module_key": "customers",
        "record_ids": "[]", "previous_record_ids": json.dumps(snapshot), "team_id": str(team.id), "owner_user_id": "__keep__"}), env.db))
    assert response.status_code == 200
    assert json.loads(accounts.access_record_options(req(env, {}), env.db, team_id=str(team.id)).body)["previous_record_ids"] == []
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        limited.query("access.records.options", {"module_key": "customers", "team_id": str(team.id)})
    with pytest.raises(HubOperationError):
        limited.execute("access.records.assign_many", {"module_key": "customers", "team_id": str(team.id),
                                                       "record_ids": "[]", "previous_record_ids": "[]"})


def test_lead_team_reconciliation_and_inactive_team(env):
    team = HubTeam(name="Leads")
    lead = HubLead(encrypted_profile_json=env.cipher.encrypt(json.dumps({"fields": {"last_name": "Saved lead"}})))
    env.db.add_all([team, lead])
    env.db.flush()
    env.access.assign_record(module_key="leads", record_id=lead.id, owner_user_id=env.sales.id, team_id=team.id)
    env.db.commit()
    data = env.service.query("access.records.options", {"module_key": "leads", "team_id": str(team.id)})
    assert data["previous_record_ids"] == [str(lead.id)]
    assert data["selected_items"][0]["name"] == "Saved lead"
    env.service.execute("access.records.assign_many", {"module_key": "leads", "team_id": str(team.id), "record_ids": "[]",
                                                      "previous_record_ids": json.dumps(data["previous_record_ids"])})
    assert env.db.scalar(select(HubRecordAssignment)).owner_user_id == env.sales.id
    team.is_active = False
    env.db.commit()
    with pytest.raises(HubOperationError, match="aktiv"):
        env.service.query("access.records.options", {"module_key": "leads", "team_id": str(team.id)})
