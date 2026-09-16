from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.hub_accounts import HubAccountService, verify_password


def test_account_user_management_uses_disclosure_tabs_in_the_management_card():
    template = Path("app/templates/account.html").read_text(encoding="utf-8")
    section = template[template.index('id="account-users"'):template.index('id="account-protocol"')]

    assert 'class="account-section account-section-stack"' in section
    assert section.count('class="account-panel account-panel-wide') == 2
    assert section.index("Benutzer anlegen") < section.index('action="/account/users"')
    assert section.index('action="/account/users"') < section.index("Benutzer verwalten")
    assert 'data-account-disclosure-group' in section
    assert 'class="account-details zoho-area account-user-area"' in section
    assert 'class="account-users-table"' not in section
    assert section.index("<summary>") < section.index('class="account-user-edit-form"')
    assert 'name="reminder_email" value="{{ account_user.reminder_email or \'\' }}"' in section
    assert 'id="account-task-reminders"' not in template
    assert 'href="#account-task-reminders"' not in template
    assert 'data-account-user-edit-cancel' in section


def test_account_user_disclosures_share_the_exclusive_accordion_behavior():
    template = Path("app/templates/account.html").read_text(encoding="utf-8")
    styles = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert '[data-account-disclosure-group] > details.account-details' in template
    assert 'button.closest("details.account-user-area")' in template
    assert ".account-user-area > summary::before" in styles
    assert ".account-user-area[open] > summary::before" in styles


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
            reminder_email=" TEAM@Example.de ",
        )
        assert updated.username == "vertretung.neu"
        assert updated.role == "viewer"
        assert updated.reminder_email == "team@example.de"
        assert verify_password("ein-neues-passwort", updated.password_hash)

        service.update_user(
            user_id=updated.id,
            username=updated.username,
            role=updated.role,
            reminder_email="",
        )
        assert updated.reminder_email is None

        deleted_username, image_storage_keys = service.delete_user(user_id=updated.id)
        assert deleted_username == "vertretung.neu"
        assert image_storage_keys == ()
        assert service.get_user(updated.id) is None


def test_hub_user_update_rejects_an_invalid_reminder_email():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        service = HubAccountService(db=db, app_secret_key="a" * 32)
        user = HubUser(username="mitarbeiter", password_hash="hash", role="viewer")
        db.add(user)
        db.commit()

        with pytest.raises(ValueError, match="gültige E-Mail-Adresse"):
            service.update_user(
                user_id=user.id,
                username=user.username,
                role=user.role,
                reminder_email="ungueltig",
            )
