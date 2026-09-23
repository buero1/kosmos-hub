"""Additive schema and one-time, unambiguous ownership backfill."""
from sqlalchemy import Column, MetaData, String, Table, inspect, select, text

TABLES = ("customer_call_activities", "customer_task_activities", "customer_meeting_activities")


def ensure_activity_responsibility_schema(engine):
    migrations = Table("hub_activity_schema_migrations", MetaData(), Column("name", String(100), primary_key=True))
    migrations.create(engine, checkfirst=True)
    fields = {"created_by_user_id": "INTEGER NULL", "assignee_user_id": "INTEGER NULL",
              "completed_by_user_id": "INTEGER NULL", "completed_by_name": "VARCHAR(255) NULL", "completed_at": "TIMESTAMP NULL"}
    for table in TABLES:
        if not inspect(engine).has_table(table):
            continue
        columns = {column["name"] for column in inspect(engine).get_columns(table)}
        for name, definition in fields.items():
            if name not in columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        with engine.begin() as connection:
            if connection.scalar(select(migrations.c.name).where(migrations.c.name == table)) is None:
                connection.execute(text(f"UPDATE {table} SET created_by_user_id = (SELECT id FROM hub_users WHERE username = {table}.created_by_username) WHERE created_by_user_id IS NULL"))
                connection.execute(text(f"UPDATE {table} SET assignee_user_id = (SELECT id FROM hub_users WHERE username = {table}.created_by_username AND is_active = true) WHERE assignee_user_id IS NULL"))
                connection.execute(migrations.insert().values(name=table))
        for name in ("created_by_user_id", "assignee_user_id"):
            index = f"ix_{table}_{name}"
            if index not in {item["name"] for item in inspect(engine).get_indexes(table)}:
                with engine.begin() as connection:
                    connection.execute(text(f"CREATE INDEX {index} ON {table} ({name})"))
        if engine.dialect.name in {"mysql", "postgresql"}:
            for name in ("created_by_user_id", "assignee_user_id", "completed_by_user_id"):
                if not any(key["constrained_columns"] == [name] for key in inspect(engine).get_foreign_keys(table)):
                    with engine.begin() as connection:
                        connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_{name} FOREIGN KEY ({name}) REFERENCES hub_users(id) ON DELETE SET NULL"))
    if inspect(engine).has_table("hub_role_permissions"):
        with engine.begin() as connection:
            if connection.scalar(select(migrations.c.name).where(migrations.c.name == "activity-scopes")) is None:
                # This scope was previously forced to 'all'. Keep every action checkbox unchanged.
                for role, scope in (("management", "team"), ("technician", "team"), ("employee", "assigned"), ("sales", "assigned")):
                    connection.execute(text("UPDATE hub_role_permissions SET record_scope = :scope WHERE module_key = 'activities' AND role_key = :role AND record_scope = 'all'"), {"role": role, "scope": scope})
                connection.execute(migrations.insert().values(name="activity-scopes"))
    table = "customer_task_email_reminders"
    if inspect(engine).has_table(table):
        fields = {"activity_kind": "VARCHAR(16) NOT NULL DEFAULT 'task'", "activity_id": "INTEGER NULL",
                  "reminder_key": "VARCHAR(64) NOT NULL DEFAULT 'primary'", "recipient_user_id": "INTEGER NULL"}
        columns = {column["name"] for column in inspect(engine).get_columns(table)}
        for name, definition in fields.items():
            if name not in columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        with engine.begin() as connection:
            if connection.scalar(select(migrations.c.name).where(migrations.c.name == "email-recipients")) is None:
                connection.execute(text(f"UPDATE {table} SET activity_id = task_id, recipient_user_id = (SELECT assignee_user_id FROM customer_task_activities WHERE id = {table}.task_id) WHERE task_id IS NOT NULL"))
                connection.execute(migrations.insert().values(name="email-recipients"))
        if "uq_activity_email_reminder" not in {item["name"] for item in inspect(engine).get_indexes(table)}:
            with engine.begin() as connection:
                connection.execute(text(f"CREATE UNIQUE INDEX uq_activity_email_reminder ON {table} (activity_kind, activity_id, reminder_key)"))
        if engine.dialect.name in {"mysql", "postgresql"} and not any(
                key["constrained_columns"] == ["recipient_user_id"] for key in inspect(engine).get_foreign_keys(table)):
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT fk_activity_email_recipient FOREIGN KEY (recipient_user_id) REFERENCES hub_users(id) ON DELETE SET NULL"))
