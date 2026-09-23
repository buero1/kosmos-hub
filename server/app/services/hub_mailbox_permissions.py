"""One authorization boundary for mailbox use by humans, agents and delivery jobs."""
import json

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, select

from app.core.mailbox_actor import resolve_mailbox_actor
from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_permission import HubMailboxMembership, HubMailboxPermission
from app.models.hub_user import HubUser
from app.models.hub_access_control import HubTeam
from app.services.hub_access_control import HubAccessControlService


MAILBOX_ACTIONS = ("view", "create", "edit", "send", "delete")
MAILBOX_ACTION_LABELS = {"view": "Lesen", "create": "Entwürfe anlegen", "edit": "Bearbeiten", "send": "Senden", "delete": "Löschen"}


def membership_column(record):
    return getattr(HubMailboxMembership, {
        "customer_zoho_emails": "customer_email_id", "hub_mailbox_emails": "mailbox_email_id",
        "hub_lead_emails": "lead_email_id", "hub_scheduled_emails": "scheduled_email_id",
    }[record.__tablename__])


def bind_message(db, cipher, record, *, account_id=None, replace=False):
    """Persist mailbox provenance at ingestion/save, never infer grants during reads."""
    from app.services.hub_email_associations import addresses
    column = membership_column(record)
    if account_id is None:
        try:
            payload = json.loads(cipher.decrypt(record.encrypted_payload_json))
        except (ValueError, TypeError, InvalidToken):
            return False
        if not isinstance(payload, dict):
            return False
        source = payload.get("mittwald_mailbox")
        if not source:
            outbound = getattr(record, "direction", "outbound") == "outbound"
            source = (payload.get("sender_email") or payload.get("absender") or payload.get("sender") or payload.get("from")) if outbound else (payload.get("to") or payload.get("empfaenger") or payload.get("empfänger") or payload.get("recipient"))
        candidates = list(db.scalars(select(HubMailboxAccount.id).where(HubMailboxAccount.email_address.in_(addresses(source)))))
        if len(candidates) != 1:
            return False
        account_id = candidates[0]
    if replace:
        db.execute(delete(HubMailboxMembership).where(column == record.id))
    existing = db.scalar(select(HubMailboxMembership.id).where(column == record.id, HubMailboxMembership.mailbox_account_id == account_id))
    if existing is None:
        db.add(HubMailboxMembership(mailbox_account_id=account_id, **{column.key: record.id}))
        db.flush()
    return True


class MailboxPermissions:
    def __init__(self, *, db, actor=None, user=None, account_id=None):
        self.db = db
        self.actor = resolve_mailbox_actor(actor)
        self.user = user
        if self.user is None and self.actor:
            self.user = db.scalar(select(HubUser).where(HubUser.username == self.actor, HubUser.is_active.is_(True)))
        self.access = HubAccessControlService(db=db)
        self._grants = None
        self._role_actions = None
        self._message_ids = {}
        self.account_id = account_id
        if account_id is not None and (db.get(HubMailboxAccount, account_id) is None or not self.can(account_id, "view")):
            raise ValueError("Das Postfach ist nicht verfuegbar.")

    def list_accounts(self):
        return [{"id": row.id, "email": row.email_address, "name": row.display_name,
                 "enabled": row.enabled, "verified": row.verified_at is not None,
                 **{f"can_{action}": self.can(row.id, action) for action in MAILBOX_ACTIONS}}
                for row in self.db.scalars(select(HubMailboxAccount).order_by(HubMailboxAccount.email_address))
                if self.can(row.id, "view")]

    def selected_account_ids(self, action="view"):
        allowed = self.account_ids(action)
        return allowed if self.account_id is None else allowed & {self.account_id}

    def _rows(self):
        if self._grants is None:
            self._grants = {"user": {}, "team": {}}
            if self.user is not None:
                for row in self.db.scalars(select(HubMailboxPermission).where(HubMailboxPermission.user_id == self.user.id)):
                    self._grants["user"][row.mailbox_account_id] = row
                team = self.db.get(HubTeam, self.user.team_id) if self.user.team_id else None
                if team and team.is_active:
                    for row in self.db.scalars(select(HubMailboxPermission).where(HubMailboxPermission.team_id == team.id)):
                        self._grants["team"][row.mailbox_account_id] = row
        return self._grants

    def granted(self, account_id, action):
        rows = self._rows()
        own, team = rows["user"].get(account_id), rows["team"].get(account_id)
        value = getattr(own, f"can_{action}", None)
        return bool(getattr(team, f"can_{action}", False) if value is None else value)

    def can(self, account_id, action="view"):
        if self.user is None or not self.user.is_active or action not in MAILBOX_ACTIONS:
            return False
        if self.user.role == "admin":
            return True
        if self._role_actions is None:
            self._role_actions = {action: self.access.can(self.user, "emails", action) for action in MAILBOX_ACTIONS}
        if not self._role_actions["view"] or not self._role_actions[action]:
            return False
        return self.granted(account_id, "view") and self.granted(account_id, action)

    def account_ids(self, action="view"):
        return {row.id for row in self.db.scalars(select(HubMailboxAccount)) if self.can(row.id, action)}

    def message_allowed(self, record, action="view"):
        if self.account_id is None and self.user is not None and self.user.is_active and self.user.role == "admin":
            return True
        column = membership_column(record)
        key = (column.key, action)
        if key not in self._message_ids:
            self._message_ids[key] = set(self.db.scalars(select(column).where(column.is_not(None),
                HubMailboxMembership.mailbox_account_id.in_(self.selected_account_ids(action)))))
        return record.id in self._message_ids[key]

    def filter_statement(self, statement, model, action="view"):
        if self.account_id is None and self.user is not None and self.user.is_active and self.user.role == "admin":
            return statement
        column = membership_column(model)
        return statement.where(model.id.in_(select(column).where(HubMailboxMembership.mailbox_account_id.in_(self.selected_account_ids(action)))))

    def require_sender(self, email_address, action="send"):
        account = self.db.scalar(select(HubMailboxAccount).where(HubMailboxAccount.email_address == email_address.strip().casefold(),
            HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None)))
        if account is None or not self.can(account.id, action):
            raise ValueError("Für dieses Absenderpostfach fehlt die Berechtigung.")
        return account

    def default_sender(self, *, for_send=False):
        actions = ("send",) if for_send else ("create", "edit", "send")
        accounts = [row for row in self.db.scalars(select(HubMailboxAccount).where(
            HubMailboxAccount.enabled.is_(True), HubMailboxAccount.verified_at.is_not(None)))
            if any(self.can(row.id, action) for action in actions)]
        preferred = getattr(self.user, "default_sender_account_id", None)
        accounts.sort(key=lambda row: (row.id != preferred, row.email_address != "info@kosmos-medien.de", row.email_address))
        return accounts[0].email_address if accounts else ""


def save_mailbox_permissions(db, *, actor, subject_type, subject_id, entries):
    admin = db.scalar(select(HubUser).where(HubUser.username == actor, HubUser.is_active.is_(True)))
    if admin is None or admin.role != "admin":
        raise ValueError("Postfachfreigaben dürfen nur Administratoren ändern.")
    model = {"user": HubUser, "team": HubTeam}.get(subject_type)
    if model is None or db.get(model, subject_id) is None:
        raise ValueError("Benutzer oder Team wurde nicht gefunden.")
    if not isinstance(entries, dict) or len(entries) > 500:
        raise ValueError("Ungültige Postfachfreigaben.")
    accounts = set(db.scalars(select(HubMailboxAccount.id)))
    normalized = {}
    for raw_id, values in entries.items():
        if not isinstance(raw_id, str) or not raw_id.isdecimal() or int(raw_id) not in accounts or not isinstance(values, dict) or set(values) != set(MAILBOX_ACTIONS):
            raise ValueError("Ungültiges Postfach oder unvollständige Rechte.")
        if any(value is not None and type(value) is not bool for value in values.values()) or (subject_type == "team" and any(value is None for value in values.values())):
            raise ValueError("Rechte müssen erlaubt, gesperrt oder (beim Benutzer) geerbt sein.")
        normalized[int(raw_id)] = values
    column = getattr(HubMailboxPermission, f"{subject_type}_id")
    # Only submitted mailboxes change; concurrent creation of a new mailbox stays denied.
    for account_id, values in normalized.items():
        row = db.scalar(select(HubMailboxPermission).where(column == subject_id, HubMailboxPermission.mailbox_account_id == account_id).with_for_update())
        if row is None:
            row = HubMailboxPermission(mailbox_account_id=account_id, **{column.key: subject_id})
            db.add(row)
        for action, value in values.items():
            setattr(row, f"can_{action}", value)
    db.flush()


def mailbox_access_snapshot(db):
    accounts = list(db.scalars(select(HubMailboxAccount).order_by(HubMailboxAccount.email_address)))
    grants = list(db.scalars(select(HubMailboxPermission)))
    policies = {user.id: MailboxPermissions(db=db, user=user) for user in db.scalars(select(HubUser))}
    return {"accounts": [{"id": row.id, "email": row.email_address, "enabled": row.enabled, "verified": row.verified_at is not None} for row in accounts],
        "users": {user.id: {row.mailbox_account_id: {action: getattr(row, f"can_{action}") for action in MAILBOX_ACTIONS} for row in grants if row.user_id == user.id} for user in db.scalars(select(HubUser))},
        "teams": {team.id: {row.mailbox_account_id: {action: getattr(row, f"can_{action}") for action in MAILBOX_ACTIONS} for row in grants if row.team_id == team.id} for team in db.scalars(select(HubTeam))},
        "effective": {user_id: {row.id: {action: policy.can(row.id, action) for action in MAILBOX_ACTIONS} for row in accounts} for user_id, policy in policies.items()}}
