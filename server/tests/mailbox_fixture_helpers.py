"""Explicit mailbox setup for tests that exercise unrelated CRM/mail behavior."""
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.hub_mailbox_account import HubMailboxAccount
from app.models.hub_mailbox_permission import HubMailboxPermission
from app.models.hub_user import HubUser


def mailbox_account(db, cipher, email="info@kosmos-medien.de", *, usernames=()):
    account = db.scalar(select(HubMailboxAccount).where(HubMailboxAccount.email_address == email))
    if account is None:
        account = HubMailboxAccount(email_address=email, username=email, display_name="Team",
            encrypted_password=cipher.encrypt("test-only"), enabled=True, verified_at=datetime.now(UTC))
        db.add(account)
        db.flush()
    for username in usernames:
        user = db.scalar(select(HubUser).where(HubUser.username == username))
        assert user is not None, "Create the test user before granting mailbox access"
        grant = db.scalar(select(HubMailboxPermission).where(HubMailboxPermission.user_id == user.id,
            HubMailboxPermission.mailbox_account_id == account.id))
        if grant is None:
            db.add(HubMailboxPermission(user_id=user.id, mailbox_account_id=account.id,
                can_view=True, can_create=True, can_edit=True, can_send=True, can_delete=True))
    db.flush()
    return account
