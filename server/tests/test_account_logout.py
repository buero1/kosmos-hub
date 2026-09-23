import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes.accounts import logout
from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.hub_user import HubUser


@pytest.mark.parametrize("role", ["admin", "management", "viewer"])
def test_logout_clears_only_current_session_and_keeps_other_sessions_valid(role):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="staff", first_name="Staff", last_name="Member", role=role, password_hash="hash")
        db.add(user)
        db.flush()
        session_version = user.session_version
        request = Request({"type": "http", "state": {"hub_user": user},
            "session": {"user_id": user.id, "csrf_token": "valid", "session_version": session_version}})
        response = logout(request=request, db=db, csrf_token="valid")
        assert response.status_code == 303
        assert response.headers['location'] == '/account/login'
        assert request.session == {}
        assert user.session_version == session_version
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "logout"))
        assert audit.actor == "staff" and audit.result == "success"
    engine.dispose()


@pytest.mark.parametrize("token", ["", "wrong"])
def test_logout_rejects_missing_or_invalid_csrf_without_ending_session(token):
    request = Request({"type": "http", "session": {"user_id": 1, "csrf_token": "valid"}})
    with pytest.raises(HTTPException) as exc:
        logout(request=request, db=None, csrf_token=token)
    assert exc.value.status_code == 403
    assert request.session['user_id'] == 1
