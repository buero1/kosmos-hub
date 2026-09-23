"""Read provenance without inventing original dates or historical employees."""
import json
from datetime import datetime

from cryptography.fernet import InvalidToken
from sqlalchemy import inspect, select
from sqlalchemy.orm import object_session

from app.db.record_info_tracking import is_imported
from app.models.hub_record_info import HubRecordInfo


def _date(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def _source_created_at(record):
    direct = getattr(record, "zoho_created_at", None)
    if direct:
        return direct
    encrypted = getattr(record, "encrypted_profile_json", None) or getattr(record, "encrypted_fields_json", None)
    if encrypted:
        from app.core.security import get_secret_cipher
        try:
            values = json.loads(get_secret_cipher().decrypt(encrypted))
            if isinstance(values, dict):
                values = {**values, **(values.get("fields") if isinstance(values.get("fields"), dict) else {})}
                for name in ("created_at_source", "Created_Time", "created_time"):
                    value = _date(values.get(name))
                    if value:
                        return value
        except (InvalidToken, ValueError, TypeError):
            pass
    return None


def actor_label(name, origin=None):
    label = name or "Nicht bekannt"
    if label == "system":
        label = "System"
    return f"{label} · über Hub-Agent" if origin == "agent" else label


def record_info(record):
    db = object_session(record) if inspect(record, raiseerr=False) is not None else None
    metadata = None
    if db is not None and record.id:
        with db.no_autoflush:
            metadata = db.execute(select(HubRecordInfo.__table__).where(
                HubRecordInfo.record_table == record.__tablename__, HubRecordInfo.record_id == record.id)).mappings().first()
    metadata = metadata or {}
    created = metadata.get("created_at") or _source_created_at(record)
    imported = is_imported(record)
    created_label = "Erstellt am"
    if created is None:
        created = getattr(record, "created_at", None)
        if imported:
            created_label = "Im Hub seit"
    return {
        "created_label": created_label, "created_at": created,
        "created_by": actor_label(metadata.get("created_name"), metadata.get("created_origin")),
        "changed_at": metadata.get("changed_at") or getattr(record, "zoho_modified_at", None),
        "changed_by": actor_label(metadata.get("changed_name"), metadata.get("changed_origin")),
    }


def record_author(record, fallback=None):
    label = record_info(record)["created_by"]
    return label if label != "Nicht bekannt" else (fallback or "Nicht bekannt")
