from contextlib import nullcontext
from datetime import datetime
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.api.routes import web
from app.models.customer import Customer
from app.models.hub_lead import HubLead
from app.models.customer_activity_reminder_notification import CustomerActivityReminderNotification
from app.models.customer_task_email_reminder import CustomerTaskEmailReminder
from app.services.hub_activity_responsibility import ACTIVITY_MODELS
from test_activity_responsibility import env, create


def request(env, monkeypatch, kind, activity_id, actor='admin'):
    monkeypatch.setattr(web, 'SessionLocal', lambda: nullcontext(env.db))
    result = Request({'type':'http', 'method':'POST', 'scheme':'http', 'server':('test', 80),
        'path':f'/activities/{kind}/{activity_id}/delete', 'headers':[], 'query_string':b'',
        'session':{'csrf_token':'valid-token'}})
    result.state.hub_user = env.users[actor]
    return result


@pytest.mark.parametrize('kind', ACTIVITY_MODELS)
@pytest.mark.parametrize('relation', ['customer', 'lead', 'none'])
def test_panel_deletion_uses_shared_service_and_cancels_only_its_reminders(env, monkeypatch, kind, relation):
    lead = HubLead(encrypted_profile_json=env.cipher.encrypt('{"fields":{}}'))
    env.db.add(lead)
    env.db.flush()
    relation_values = {'customer_id':str(env.customer.id)} if relation == 'customer' else {'lead_id':str(lead.id)} if relation == 'lead' else {}
    channels = {'reminder_channel':'email', 'reminder_minutes_before':'5'} if kind == 'task' else {
        'reminder_channels':'popup,email', 'reminder_minutes_before':'5,5'}
    row = create(env, kind, **relation_values, **channels)
    other = create(env, kind)
    identity = row.id
    env.db.add(CustomerActivityReminderNotification(user_id=env.users['admin'].id, activity_kind=kind,
        activity_id=identity, reminder_key='primary', remind_at=datetime(2030, 10, 15, 6, 55)))
    env.db.commit()
    response = web.delete_activity_from_panel(kind, identity, request(env, monkeypatch, kind, identity), env.db, csrf_token='valid-token')
    assert response.status_code == 200 and json.loads(response.body)['ok']
    assert env.db.get(ACTIVITY_MODELS[kind], identity) is None
    assert env.db.get(ACTIVITY_MODELS[kind], other.id) is not None
    assert not list(env.db.scalars(select(CustomerActivityReminderNotification)))
    job = env.db.scalars(select(CustomerTaskEmailReminder)).one()
    assert job.status == 'cancelled' and job.activity_id is None and job.task_id is None
    assert env.db.get(Customer, env.customer.id) and env.db.get(HubLead, lead.id)


@pytest.mark.parametrize('kind', ACTIVITY_MODELS)
def test_panel_deletion_rechecks_current_permissions_and_csrf(env, monkeypatch, kind):
    row = create(env, kind, owner='steffi')
    env.db.commit()
    req = request(env, monkeypatch, kind, row.id)
    with pytest.raises(HTTPException) as exc:
        web.delete_activity_from_panel(kind, row.id, req, env.db, csrf_token='wrong-token')
    assert exc.value.status_code == 403
    env.access.permission(role_key='management', module_key='activities').can_manage = False
    env.db.commit()
    response = web.delete_activity_from_panel(kind, row.id, request(env, monkeypatch, kind, row.id, 'manager'), env.db, csrf_token='valid-token')
    assert response.status_code == 400 and 'Verwalten' in json.loads(response.body)['detail']
    assert env.db.get(ACTIVITY_MODELS[kind], row.id).status == 'planned'
    with pytest.raises(HTTPException) as exc:
        web.delete_activity_from_panel(kind, row.id, request(env, monkeypatch, kind, row.id, 'steffi'), env.db, csrf_token='valid-token')
    assert exc.value.status_code == 403
    env.access.permission(role_key='employee', module_key='activities').can_delete = True
    env.db.commit()
    response = web.delete_activity_from_panel(kind, row.id, request(env, monkeypatch, kind, row.id, 'other'), env.db, csrf_token='valid-token')
    assert response.status_code == 400
    response = web.delete_activity_from_panel(kind, row.id, request(env, monkeypatch, kind, row.id, 'steffi'), env.db, csrf_token='valid-token')
    assert response.status_code == 200


def test_panel_delete_refuses_inflight_reminder_without_changes(env, monkeypatch):
    row = create(env, 'task', reminder_channel='email', reminder_minutes_before='5')
    job = env.db.scalars(select(CustomerTaskEmailReminder)).one()
    job.status = 'sending'
    env.db.commit()
    response = web.delete_activity_from_panel('task', row.id, request(env, monkeypatch, 'task', row.id), env.db, csrf_token='valid-token')
    assert response.status_code == 400 and 'gerade versendet' in json.loads(response.body)['detail']
    assert env.db.get(ACTIVITY_MODELS['task'], row.id) and job.status == 'sending'


def test_unknown_activity_type_is_rejected(env, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        web.delete_activity_from_panel('invalid', 1, request(env, monkeypatch, 'invalid', 1), env.db, csrf_token='valid-token')
    assert exc.value.status_code == 404
