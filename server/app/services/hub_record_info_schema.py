"""Additive and restart-safe employee/provenance schema upgrade."""
from sqlalchemy import inspect, text

from app.models.hub_record_info import HubRecordInfo


def ensure_record_info_schema(engine):
    for table, fields in (
        ("hub_users", {"first_name": "VARCHAR(100) NULL", "last_name": "VARCHAR(100) NULL"}),
        ("hub_activity_events", {"actor_user_id": "INT NULL", "actor_name": "VARCHAR(255) NULL"}),
        ("audit_log", {"actor_user_id": "INT NULL", "actor_name": "VARCHAR(255) NULL"}),
    ):
        inspector = inspect(engine)
        if not inspector.has_table(table):
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in fields.items():
            if name not in columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        if table != "hub_users" and engine.dialect.name in {"mysql", "postgresql"}:
            if not any(key["constrained_columns"] == ["actor_user_id"] for key in inspect(engine).get_foreign_keys(table)):
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_actor_user_id_hub_users "
                                            "FOREIGN KEY (actor_user_id) REFERENCES hub_users (id) ON DELETE SET NULL"))
    HubRecordInfo.__table__.create(bind=engine, checkfirst=True)
