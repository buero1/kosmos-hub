"""Additive, restart-safe mailbox authorization migration."""
from sqlalchemy import inspect, text
from app.models.hub_mailbox_permission import HubMailboxMembership, HubMailboxPermission


def ensure_mailbox_permission_schema(engine):
    inspector = inspect(engine)
    for table, fields in (
        ("hub_users", {"email_address": "VARCHAR(320) NULL", "default_sender_account_id": "INT NULL"}),
        ("hub_role_permissions", {"can_send": "BOOLEAN NOT NULL DEFAULT FALSE"}),
    ):
        columns = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in fields.items():
            if name not in columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
                    if name == "can_send":
                        connection.execute(text("UPDATE hub_role_permissions SET can_send = can_create WHERE module_key = 'emails'"))
    HubMailboxPermission.__table__.create(bind=engine, checkfirst=True)
    HubMailboxMembership.__table__.create(bind=engine, checkfirst=True)
    if engine.dialect.name in {"mysql", "postgresql"}:
        keys = inspect(engine).get_foreign_keys("hub_users")
        if not any(key["constrained_columns"] == ["default_sender_account_id"] for key in keys):
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE hub_users ADD CONSTRAINT fk_hub_user_default_sender "
                    "FOREIGN KEY (default_sender_account_id) REFERENCES hub_mailbox_accounts (id) ON DELETE SET NULL"))
