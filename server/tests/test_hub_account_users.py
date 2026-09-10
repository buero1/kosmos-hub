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


def test_last_admin_cannot_be_deleted_or_changed_to_a_viewer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        admin = HubUser(username="kosmosadmin", password_hash="hash", role="admin")
        db.add(admin)
        db.commit()

        with pytest.raises(ValueError, match="letzte Administrator"):
            service.delete_user(user_id=admin.id)
        with pytest.raises(ValueError, match="letzte Administrator"):
            service.update_user(
                user_id=admin.id,
                username="kosmosadmin",
                role="viewer",
            )


def test_admin_can_edit_and_delete_another_user_when_an_admin_remains():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        admin = HubUser(username="kosmosadmin", password_hash="hash", role="admin")
        other_admin = HubUser(username="vertretung", password_hash="hash", role="admin")
        db.add_all((admin, other_admin))
        db.commit()

        updated = service.update_user(
            user_id=other_admin.id,
            username="vertretung.neu",
            role="viewer",
            password="ein-neues-passwort",
            password_confirmation="ein-neues-passwort",
        )
        assert updated.username == "vertretung.neu"
        assert updated.role == "viewer"
        assert verify_password("ein-neues-passwort", updated.password_hash)

        deleted_username, image_storage_keys = service.delete_user(user_id=updated.id)
        assert deleted_username == "vertretung.neu"
        assert image_storage_keys == ()
        assert service.get_user(updated.id) is None
