"""Shared administrative boundaries. Never serialize account/credential ORM objects."""
from inspect import signature
import math

from sqlalchemy import select

from app.core.config import get_settings
from app.models.hub_user import HubUser
from app.models.hub_mailbox_imap_sync_state import HubMailboxImapSyncState
from app.models.email_ai_prompt_preset import EmailAiPromptPreset
from app.services.hub_access_control import HubAccessControlService, ACCESS_MODULES, ACCESS_ACTIONS, ACCESS_SCOPES
from app.services.hub_accounts import HubAccountService
from app.services.hub_operations import HubOperationError, HubOperationService
from app.services.email_compose_images import EmailComposeImageService
from app.services.email_composer_settings import EmailComposerSettingsService
from app.services.email_ai_prompt_presets import EmailAiPromptPresetService, DEFAULT_EMAIL_AI_PROMPT_PRESETS
from app.services.fleet_refresh_settings import FleetRefreshSettingsService
from app.services.styling_settings import StylingSettingsService
from app.services.customer_communications import CustomerCommunicationService
from app.services.hub_mailbox_accounts import HubMailboxAccountService


SETTING_SERVICES = {"styling": StylingSettingsService, "email_composer": EmailComposerSettingsService, "fleet_refresh": FleetRefreshSettingsService}


def setting_parameters(section):
    return {name: parameter for name, parameter in signature(SETTING_SERVICES[section].configure).parameters.items()
            if name not in {"self", "actor", "now"}}


def runtime_settings(db, section):
    return SETTING_SERVICES[section](db=db).get_runtime_settings()


def typed_setting(parameter, value):
    kind = parameter.annotation
    try:
        if kind is bool:
            if value not in (True, False, "true", "false", "1", "0"):
                raise ValueError()
            return value in (True, "true", "1")
        if kind is int:
            if isinstance(value, bool) or str(int(value)) != str(value):
                raise ValueError()
            return int(value)
        if kind is float:
            result = float(value)
            if not math.isfinite(result):
                raise ValueError()
            return result
        if not isinstance(value, str):
            raise ValueError()
        return value
    except (TypeError, ValueError, OverflowError) as exc:
        raise HubOperationError(f"Ungueltiger Wert fuer {parameter.name}.") from exc


class HubAdministrationService:
    def __init__(self, *, db, cipher, actor):
        self.db, self.cipher, self.actor = db, cipher, actor

    def user(self, *, admin=True):
        user = self.db.scalar(select(HubUser).where(HubUser.username == self.actor, HubUser.is_active.is_(True)).execution_options(populate_existing=True))
        if user is None or admin and user.role != "admin":
            raise HubOperationError("Fuer diese Verwaltung fehlt die Berechtigung.")
        return user

    def accounts(self):
        return HubAccountService(db=self.db, app_secret_key=get_settings().app_secret_key)

    def configure(self, section, **values):
        actor = self.user()
        if section not in SETTING_SERVICES:
            raise HubOperationError("Unbekannter Einstellungsbereich.")
        parameters = setting_parameters(section)
        if set(values) - parameters.keys():
            raise HubOperationError("Unbekannte Einstellung.")
        current = runtime_settings(self.db, section)
        merged = {name: typed_setting(param, values.get(name, getattr(current, name))) for name, param in parameters.items()}
        domain = SETTING_SERVICES[section](db=self.db)
        if "actor" in signature(domain.configure).parameters:
            merged["actor"] = actor
        return domain.configure(**merged)

    def configure_signature(self, *, signature_html):
        self.user()
        normalized = signature_html.strip()
        if normalized:
            normalized = CustomerCommunicationService._sanitized_email_content(normalized)
        return EmailComposerSettingsService(db=self.db).configure_signature(signature_html=normalized)

    def configure_reminder_email(self, *, reminder_email):
        user = self.user(admin=False)
        self.accounts().configure_reminder_email(user=user, reminder_email=reminder_email)
        return user

    def configure_mailbox_alert(self, *, mailbox_alert_email):
        user = self.user()
        domain = HubMailboxAccountService(db=self.db, cipher=self.cipher)
        address = domain._email_address(mailbox_alert_email)
        if address in {status.email_address.casefold() for status in domain.list_statuses()}:
            raise HubOperationError("Die Warnadresse muss ausserhalb der ueberwachten Postfaecher liegen.")
        user.mailbox_alert_email = address
        for state in self.db.scalars(select(HubMailboxImapSyncState).where(HubMailboxImapSyncState.folder == "INBOX")):
            if state.last_error or state.consecutive_failures:
                state.alerted_at = None
        self.db.flush()
        return user

    def prompt_presets(self):
        self.user()
        return tuple(self.db.scalars(select(EmailAiPromptPreset).order_by(EmailAiPromptPreset.sort_order, EmailAiPromptPreset.id)))

    def save_prompt_presets(self, *, presets, new_label="", new_instruction=""):
        actor = self.user()
        domain = EmailAiPromptPresetService(db=self.db)
        with self.db.begin_nested():
            domain.ensure_default_presets()
            domain.update_presets(actor=actor, presets=presets)
            if new_label.strip() or new_instruction.strip():
                domain.add_preset(actor=actor, label=new_label, instruction=new_instruction)

    def access_snapshot(self):
        self.user()
        access = HubAccessControlService(db=self.db)
        return {"roles": access.list_roles(), "teams": access.list_teams(), "permissions": access.permission_matrix(),
                "users": self.accounts().list_users(), "assignments": access.list_assignments(), "grants": access.list_grants()}

    def mailbox_access_snapshot(self):
        self.user()
        from app.services.hub_mailbox_permissions import mailbox_access_snapshot
        return mailbox_access_snapshot(self.db)

    def save_mailbox_access(self, *, subject_type, subject_id, entries):
        self.user()
        from app.services.hub_mailbox_permissions import save_mailbox_permissions
        return save_mailbox_permissions(self.db, actor=self.actor, subject_type=subject_type, subject_id=subject_id, entries=entries)

    def save_role(self, *, role_key, name, description, permissions):
        self.user()
        allowed = {module.key for module in ACCESS_MODULES}
        if not isinstance(permissions, dict) or set(permissions) - allowed:
            raise HubOperationError("Unbekannte Berechtigungsmodule.")
        for entry in permissions.values():
            if not isinstance(entry, dict) or set(entry) - {*ACCESS_ACTIONS, "scope"} or any(type(v) is not bool for k, v in entry.items() if k != "scope"):
                raise HubOperationError("Berechtigungen muessen boolesche Werte enthalten.")
            if entry.get("scope", "none") not in ACCESS_SCOPES:
                raise HubOperationError("Ungueltiger Datensatzbereich.")
        access = HubAccessControlService(db=self.db)
        key = access.normalize_role_key(role_key or name)
        current = access.permission_matrix().get(key, {})
        merged = {module.key: {**({**{action: bool(getattr(current[module.key], f"can_{action}")) for action in ACCESS_ACTIONS},
                                  "scope": current[module.key].record_scope} if module.key in current else {}),
                               **permissions.get(module.key, {})} for module in ACCESS_MODULES}
        with self.db.begin_nested():
            return access.save_role(role_key=role_key, name=name, description=description, permissions=merged)

    def delete_role(self, role_key):
        self.user()
        return HubAccessControlService(db=self.db).delete_role(role_key)

    def create_team(self, **values):
        self.user()
        return HubAccessControlService(db=self.db).create_team(**values)

    def update_team(self, **values):
        self.user()
        return HubAccessControlService(db=self.db).update_team(**values)

    def delete_team(self, team_id):
        self.user()
        return HubAccessControlService(db=self.db).delete_team(team_id)

    def assign_record(self, **values):
        self.user()
        return HubAccessControlService(db=self.db).assign_record(**values)

    def add_grant(self, **values):
        self.user()
        return HubAccessControlService(db=self.db).add_grant(**values)

    def delete_grant(self, grant_id):
        self.user()
        return HubAccessControlService(db=self.db).delete_grant(grant_id)

    def create_user(self, **values):
        self.user()
        return self.accounts().create_user(**values)

    def update_user(self, *, user_id, **values):
        self.user()
        # Serialize demotion/deletion so two concurrent changes cannot remove the last admin.
        admins = self.db.scalars(select(HubUser).where(HubUser.role == "admin").order_by(HubUser.id).with_for_update().execution_options(populate_existing=True)).all()
        self.user()
        user = self.accounts().get_user(user_id)
        if user is None:
            raise HubOperationError("Dieser Hub-Benutzer wurde nicht gefunden.")
        if user.role == "admin" and values.get("role", user.role).strip().casefold() != "admin" and sum(row.is_active for row in admins) <= 1:
            raise HubOperationError("Der letzte Administrator kann nicht herabgestuft werden.")
        return self.accounts().update_user(user_id=user_id, **{
            "username": user.username, "role": user.role, "team_id": user.team_id, **values})

    def delete_user(self, *, user_id):
        self.user()
        admins = self.db.scalars(select(HubUser).where(HubUser.role == "admin").order_by(HubUser.id).with_for_update().execution_options(populate_existing=True)).all()
        self.user()
        if any(row.id == user_id for row in admins) and sum(row.is_active for row in admins) <= 1:
            raise HubOperationError("Der letzte Administrator kann nicht geloescht werden.")
        name, keys = self.accounts().delete_user(user_id=user_id)
        storage = EmailComposeImageService(db=self.db, cipher=self.cipher).storage
        gateway = HubOperationService(db=self.db, cipher=self.cipher, actor=self.actor)
        for key in keys:
            gateway.after_commit(f"delete-user-image:{key}", lambda key=key: storage.remove(key))
        return name, ()
