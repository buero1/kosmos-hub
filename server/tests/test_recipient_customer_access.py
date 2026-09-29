import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from test_hub_mailbox_operations import env
from app.api.routes import web
from app.models.customer_contact import CustomerContact
from app.services.hub_access_control import HubAccessControlService
from app.services.hub_email_readers import recipients
from app.services.hub_operations import HubOperationError


def request(env, user):
    result = Request({"type": "http", "method": "GET", "path": "/emails/compose/recipients",
                      "headers": [], "query_string": b"", "session": {}})
    result.state.hub_user = user
    return result


def contact(env, customer, prefix):
    row = CustomerContact(customer_id=customer.id, encrypted_profile_json=env.cipher.encrypt(json.dumps({
        "fields": {"Name": prefix, "E-Mail": f"{prefix}@example.test",
                   "Zweite E-Mail-Adresse": f"{prefix}.other@example.test"},
    })))
    env.db.add(row)
    env.db.flush()
    return row


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("actor", ["admin", "sales"])
def test_authorized_customer_recipients_ignore_old_visibility_and_status(env, visible, actor):
    env.own.is_visible = visible
    env.own.zoho_status = "Sonstige"
    env.own.encrypted_profile_json = env.cipher.encrypt(json.dumps({"fields": {"Status": "Sonstige", "Kunde Typ": "Prospect"}}))
    own_contact = contact(env, env.own, "find-own")
    contact(env, env.hidden, "find-other")
    env.db.commit()
    gateway, user = (env.service, env.admin) if actor == "admin" else (env.limited, env.sales)
    snapshot = (env.own.is_visible, env.own.zoho_status, env.own.encrypted_profile_json)

    expected = {"find-own@example.test", "find-own.other@example.test"}
    found = recipients(gateway, customer_id=env.own.id)
    assert {item["email"] for item in found} == expected
    assert all(item["key"].startswith(f"contact:{own_contact.id}:") for item in found)
    ui = web.mailbox_compose_customer_contact_recipients(env.own.id, request(env, user), env.db)
    assert ui["recipients"] == found
    search = gateway.query("emails.recipients.search", {"query": "find-own"})["recipients"]
    assert {item["email"] for item in search} == expected
    assert web.mailbox_compose_recipients(request(env, user), env.db, q="find-own")["recipients"] == search
    assert snapshot == (env.own.is_visible, env.own.zoho_status, env.own.encrypted_profile_json)
    assert not env.db.new and not env.db.dirty


@pytest.mark.parametrize("visible", [False, True])
def test_denied_and_missing_customers_stay_inaccessible(env, visible):
    env.hidden.is_visible = visible
    contact(env, env.hidden, "find-secret")
    env.db.commit()
    for customer_id in (env.hidden.id, 999999):
        with pytest.raises(HubOperationError, match="nicht verfuegbar"):
            recipients(env.limited, customer_id=customer_id)
        with pytest.raises(HTTPException) as error:
            web.mailbox_compose_customer_contact_recipients(customer_id, request(env, env.sales), env.db)
        assert error.value.status_code == 404
    assert env.limited.query("emails.recipients.search", {"query": "find-secret"})["recipients"] == []


@pytest.mark.parametrize("module", ["customers", "contacts", "emails"])
def test_module_permissions_still_apply_to_direct_and_search_recipients(env, module):
    env.own.is_visible = False
    contact(env, env.own, "find-own")
    access = HubAccessControlService(db=env.db)
    access.permission(role_key=env.sales.role, module_key=module).can_view = False
    env.db.commit()
    if module == "contacts":
        assert recipients(env.limited, customer_id=env.own.id) == []
    else:
        with pytest.raises(HubOperationError):
            recipients(env.limited, customer_id=env.own.id)
    if module == "emails":
        with pytest.raises(HubOperationError):
            recipients(env.limited, query="find-own")
    else:
        assert recipients(env.limited, query="find-own") == []


def test_inactive_user_cannot_read_recipients(env):
    contact(env, env.own, "find-own")
    env.sales.is_active = False
    env.db.commit()
    with pytest.raises(HubOperationError):
        recipients(env.limited, customer_id=env.own.id)
    with pytest.raises(HubOperationError):
        recipients(env.limited, query="find-own")
