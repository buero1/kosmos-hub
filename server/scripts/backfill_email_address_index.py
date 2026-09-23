"""Rebuild address lookup tokens only; never send, move or alter an email body."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.db.base import Base
from app.db.session import SessionLocal
from app.core.security import get_secret_cipher
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.services.hub_email_associations import counterpart_addresses, index_email


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write lookup tokens; default is read-only dry run")
    args = parser.parse_args()
    cipher, counts = get_secret_cipher(), {}
    with SessionLocal() as db:
        for model in (CustomerZohoEmail, HubMailboxEmail):
            checked = indexed = 0
            last_id = 0
            while True:
                rows = list(db.scalars(select(model).where(model.id > last_id).order_by(model.id).limit(250)))
                if not rows:
                    break
                for row in rows:
                    checked += 1
                    last_id = row.id
                    if row.mailbox_state == "draft" or getattr(row, "sync_status", "sent") in {"failed", "pending"}:
                        continue
                    payload = json.loads(cipher.decrypt(row.encrypted_payload_json))
                    indexed += bool(counterpart_addresses(payload, row.direction))
                    if args.apply:
                        index_email(db, cipher, row)
                db.flush()
                db.expunge_all()
            counts[model.__tablename__] = {"checked": checked, "with_addresses": indexed}
        if args.apply:
            db.commit()
        else:
            db.rollback()
    print(json.dumps({"applied": args.apply, "counts": counts}))


if __name__ == "__main__":
    main()
