import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_accounts import HubAccountService, verify_password


def test_admin_can_create_a_hub_user_with_a_hashed_password_and_selected_role():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        user = service.create_user(
            username="Mitarbeiter.Test",
            password="sicheres-passwort",
            password_confirmation="sicheres-passwort",
            role="viewer",
        )
        db.commit()

        assert user.username == "mitarbeiter.test"
        assert user.role == "viewer"
        assert user.password_hash != "sicheres-passwort"
        assert verify_password("sicheres-passwort", user.password_hash)
        assert [entry.username for entry in service.list_users()] == ["mitarbeiter.test"]


def test_hub_user_creation_rejects_duplicate_usernames_and_unknown_roles():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        db.add(HubUser(username="kosmosadmin", password_hash="hash", role="admin"))
        db.commit()

        with pytest.raises(ValueError, match="bereits vergeben"):
            service.create_user(
                username="KosmosAdmin",
                password="sicheres-passwort",
                password_confirmation="sicheres-passwort",
                role="admin",
            )
        with pytest.raises(ValueError, match="gültige Benutzerrolle"):
            service.create_user(
                username="neuer.user",
                password="sicheres-passwort",
                password_confirmation="sicheres-passwort",
                role="owner",
            )
