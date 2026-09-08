from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes.accounts import _account_section_for_path
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_accounts import HubAccountService


def test_desktop_device_tokens_are_separate_from_mcp_tokens_and_can_be_revoked():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(user)
        db.commit()
        service = HubAccountService(db=db, app_secret_key="a" * 32)

        device, raw_token = service.create_desktop_device(user=user, name="Arbeits-PC")

        assert raw_token.startswith("khdsk_")
        assert service.authenticate_mcp_access_token(raw_token) is None
        authenticated = service.authenticate_desktop_device(raw_token)
        assert authenticated is not None
        assert authenticated[0].id == user.id
        assert authenticated[1].id == device.id

        service.revoke_desktop_device(user=user, device_id=device.id)
        assert service.authenticate_desktop_device(raw_token) is None


def test_desktop_device_name_accepts_german_characters():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(user)
        db.commit()
        service = HubAccountService(db=db, app_secret_key="a" * 32)

        device, _ = service.create_desktop_device(user=user, name="Büro Gerät")

        assert device.name == "Büro Gerät"


def test_desktop_device_errors_stay_in_desktop_notifier_section():
    assert _account_section_for_path("/account/desktop-devices") == "account-desktop-notifier"
