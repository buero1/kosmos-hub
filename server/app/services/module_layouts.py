"""Reusable persistence and validation for global module item layouts."""

from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.hub_user import HubUser
from app.models.module_layout import ModuleLayout


class ModuleLayoutError(ValueError):
    pass


class ModuleLayoutService:
    """Stores named global layouts while safely accommodating changed item sets."""

    _LAYOUT_KEY = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
    _MAX_ITEMS = 250
    _MAX_PAYLOAD_LENGTH = 25_000

    def __init__(self, *, db: Session):
        self.db = db

    def ordered_keys(self, *, layout_key: str, default_keys: tuple[str, ...]) -> tuple[str, ...]:
        self._validate_layout_key(layout_key)
        defaults = self._unique_keys(default_keys)
        layout = self.db.scalar(select(ModuleLayout).where(ModuleLayout.layout_key == layout_key))
        stored = self._stored_keys(layout.item_order_json) if layout is not None else ()
        return self._merge_order(stored, defaults)

    def configure(
        self,
        *,
        actor: HubUser,
        layout_key: str,
        item_order_json: str,
        allowed_keys: tuple[str, ...],
    ) -> ModuleLayout:
        if actor.role != "admin":
            raise ModuleLayoutError("Only Hub administrators can change global layouts.")
        self._validate_layout_key(layout_key)
        submitted = self._submitted_keys(item_order_json)
        allowed = self._unique_keys(allowed_keys)
        if set(submitted) != set(allowed) or len(submitted) != len(allowed):
            raise ModuleLayoutError("The submitted layout does not match the available fields.")

        layout = self.db.scalar(select(ModuleLayout).where(ModuleLayout.layout_key == layout_key))
        if layout is None:
            layout = ModuleLayout(layout_key=layout_key)
            self.db.add(layout)
        layout.item_order_json = json.dumps(submitted, ensure_ascii=True, separators=(",", ":"))
        layout.configured_by_user_id = actor.id
        self.db.flush()
        return layout

    @classmethod
    def _submitted_keys(cls, item_order_json: str) -> tuple[str, ...]:
        if len(item_order_json) > cls._MAX_PAYLOAD_LENGTH:
            raise ModuleLayoutError("The layout payload is too large.")
        try:
            source = json.loads(item_order_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModuleLayoutError("The layout data is invalid.") from exc
        if not isinstance(source, list):
            raise ModuleLayoutError("The layout data is invalid.")
        keys = tuple(item for item in source if isinstance(item, str))
        if len(keys) != len(source):
            raise ModuleLayoutError("The layout data is invalid.")
        return cls._validate_item_keys(keys)

    @classmethod
    def _stored_keys(cls, item_order_json: str) -> tuple[str, ...]:
        try:
            source = json.loads(item_order_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(source, list):
            return ()
        keys = tuple(item for item in source if isinstance(item, str))
        if len(keys) != len(source):
            return ()
        try:
            return cls._validate_item_keys(keys)
        except ModuleLayoutError:
            return ()

    @staticmethod
    def _merge_order(stored_keys: tuple[str, ...], default_keys: tuple[str, ...]) -> tuple[str, ...]:
        default_set = set(default_keys)
        stored = tuple(key for key in stored_keys if key in default_set)
        return stored + tuple(key for key in default_keys if key not in set(stored))

    @classmethod
    def _unique_keys(cls, keys: tuple[str, ...]) -> tuple[str, ...]:
        return cls._validate_item_keys(keys, reject_duplicates=False)

    @classmethod
    def _validate_item_keys(cls, keys: tuple[str, ...], *, reject_duplicates: bool = True) -> tuple[str, ...]:
        if len(keys) > cls._MAX_ITEMS or any(not key or len(key) > 128 for key in keys):
            raise ModuleLayoutError("The layout contains invalid fields.")
        unique = tuple(dict.fromkeys(keys))
        if reject_duplicates and len(unique) != len(keys):
            raise ModuleLayoutError("The layout contains duplicate fields.")
        return unique

    @classmethod
    def _validate_layout_key(cls, layout_key: str) -> None:
        if not cls._LAYOUT_KEY.fullmatch(layout_key):
            raise ModuleLayoutError("The layout key is invalid.")
