from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_accounts import HubAccountService, hash_password
from app.services.site_mcp_proxy import SiteMcpProxyService


def test_mcp_token_can_be_authenticated_and_revoked():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash=hash_password("correct-horse-battery-staple"))
        db.add(user)
        db.commit()

        service = HubAccountService(db=db, app_secret_key="a" * 32)
        access_token, raw_token = service.create_mcp_access_token(user=user, name="Codex desktop")

        authenticated = service.authenticate_mcp_access_token(raw_token)
        assert authenticated is not None
        authenticated_user, authenticated_token = authenticated
        assert authenticated_user.id == user.id
        assert authenticated_token.id == access_token.id
        assert authenticated_token.last_used_at is not None

        service.revoke_mcp_access_token(user=user, token_id=access_token.id)
        assert service.authenticate_mcp_access_token(raw_token) is None


def test_only_explicit_readonly_abilities_can_use_the_generic_execution_path():
    readonly = {
        "ability": {"meta": {"annotations": {"readonly": True, "destructive": False}}}
    }
    mutation = {
        "ability": {"meta": {"annotations": {"readonly": False, "destructive": False}}}
    }

    assert SiteMcpProxyService._ability_is_readonly(readonly) is True
    assert SiteMcpProxyService._ability_is_readonly(mutation) is False
    assert SiteMcpProxyService._ability_is_readonly({}) is False
