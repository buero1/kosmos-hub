"""One-time backfill for sender rules from messages already in Hub spam."""

import json

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from app.core.security import get_secret_cipher
from app.db.base import Base
from app.db.session import SessionLocal
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.hub_spam_senders import HubSpamSenderService


def main() -> None:
    Base.registry.configure()
    cipher = get_secret_cipher()
    added = 0
    skipped = 0
    with SessionLocal() as db:
        rules = HubSpamSenderService(db=db)
        for model in (CustomerZohoEmail, HubMailboxEmail):
            statement = select(model.encrypted_payload_json).where(
                model.mailbox_state == "spam", model.direction == "inbound"
            )
            for encrypted_payload in db.scalars(statement):
                try:
                    payload = json.loads(cipher.decrypt(encrypted_payload))
                except (InvalidToken, TypeError, UnicodeError, ValueError):
                    skipped += 1
                    continue
                if not isinstance(payload, dict):
                    skipped += 1
                    continue
                added += int(rules.block(direction="inbound", payload=payload))
        db.commit()
    print(f"Spam sender backfill: {added} rules added, {skipped} unreadable messages skipped.")


if __name__ == "__main__":
    main()
