"""Backfill unambiguous mailbox provenance without changing messages or grants."""
import json
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.core.security import get_secret_cipher
from app.models.customer_communication import CustomerZohoEmail
from app.models.hub_mailbox_email import HubMailboxEmail
from app.models.hub_lead_email import HubLeadEmail
from app.models.hub_scheduled_email import HubScheduledEmail
from app.services.hub_mailbox_permission_schema import ensure_mailbox_permission_schema
from app.services.hub_mailbox_permissions import bind_message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist membership mappings (default: rollback dry run)")
    args = parser.parse_args()
    ensure_mailbox_permission_schema(engine)
    result = {}
    with SessionLocal() as db:
        cipher = get_secret_cipher()
        for model in (CustomerZohoEmail, HubMailboxEmail, HubLeadEmail, HubScheduledEmail):
            counts = {"mapped": 0, "unresolved": 0}
            for row in db.scalars(select(model)):
                counts["mapped" if bind_message(db, cipher, row) else "unresolved"] += 1
            result[model.__tablename__] = counts
        if args.apply:
            db.commit()
        else:
            db.rollback()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
