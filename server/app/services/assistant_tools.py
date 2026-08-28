"""Validated local data tools available to the Kosmos OpenAI assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.customer import Customer
from app.services.fleet_inventory import FleetInventoryItem, FleetInventoryService
from app.services.site_users import SiteUserService

MAX_TOOL_RESULTS = 100
MAX_SEARCH_RESULTS = 5


class AssistantToolError(ValueError):
    """Raised when the model calls a tool with invalid arguments."""


@dataclass
class AssistantToolState:
    panel_scope: str
    panel_site_ids: set[int]
    selection_site_ids: tuple[int, ...] | None = None


class HubAssistantTools:
    """Expose a narrow, read-only Hub view plus validated site selection."""

    def __init__(
        self,
        *,
        db: Session,
        cipher: SecretCipher,
        panel_site_ids: set[int] | None,
    ):
        self.db = db
        self.cipher = cipher
        self.inventory = FleetInventoryService(db=db, cipher=cipher)
        self.items = self.inventory.list_items(limit=1000)
        self.items_by_id = {item.site.id: item for item in self.items}
        self.state = AssistantToolState(
            panel_scope="all" if panel_site_ids is None else "selected",
            panel_site_ids=set(panel_site_ids or ()),
        )
        self._selection_candidates: set[int] = set()

    @property
    def selection_site_ids(self) -> tuple[int, ...] | None:
        return self.state.selection_site_ids

    @property
    def latest_data_at(self) -> datetime | None:
        values = [
            snapshot.captured_at
            for item in self.items
            for snapshot in (item.snapshot, item.update_snapshot)
            if snapshot is not None
        ]
        return max(values) if values else None

    @classmethod
    def definitions(cls) -> list[dict[str, Any]]:
        """Return strict JSON-schema tools, never database access or mutations."""
        return [
            cls._tool(
                "search_customers",
                "Find likely Hub customers from an imprecise name or domain. Returns at most five safe candidates with IDs, names, status, and linked domains.",
                {
                    "query": {"type": "string", "description": "Customer name, partial name, or domain as written by the user."},
                },
            ),
            cls._tool(
                "search_components",
                "Find installed plugin or theme names despite spelling variants. Call this before filtering a named component unless an exact identifier was already returned by a tool.",
                {
                    "query": {"type": "string", "description": "Plugin or theme name as written by the user."},
                    "kind": {"type": "string", "enum": ["all", "plugin", "theme"]},
                },
            ),
            cls._tool(
                "query_sites",
                "Find websites by customer status or name prefix, site, plugin/theme installation state, and update state. Returned site IDs can be passed to set_site_selection.",
                {
                    "scope": {"type": "string", "enum": ["all", "panel"]},
                    "customer_ids": {"type": "array", "items": {"type": "integer"}},
                    "customer_status": {
                        "type": "string",
                        "enum": ["all", "Aktuell", "Neu", "gekündigt", "Kündigung liegt vor", "other", "unlinked"],
                    },
                    "customer_name_prefix": {
                        "type": "string",
                        "description": "A leading customer-name fragment such as A, or an empty string.",
                    },
                    "site_ids": {"type": "array", "items": {"type": "integer"}},
                    "component_kind": {"type": "string", "enum": ["all", "plugin", "theme"]},
                    "component_identifier": {"type": "string", "description": "Exact identifier returned by search_components, or an empty string."},
                    "component_state": {"type": "string", "enum": ["any", "installed", "active", "inactive"]},
                    "update_state": {"type": "string", "enum": ["any", "available", "not_available", "not_checked"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            ),
            cls._tool(
                "list_updates",
                "List currently stored WordPress, plugin, or theme update offers. Use customer IDs or site IDs returned from other tools; this does not refresh or install anything.",
                {
                    "scope": {"type": "string", "enum": ["all", "panel"]},
                    "customer_ids": {"type": "array", "items": {"type": "integer"}},
                    "site_ids": {"type": "array", "items": {"type": "integer"}},
                    "component_kind": {"type": "string", "enum": ["all", "wordpress", "plugin", "theme"]},
                    "component_identifier": {"type": "string", "description": "Exact identifier returned by search_components, or an empty string."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            ),
            cls._tool(
                "list_backups",
                "Read stored UpdraftPlus backup metadata for selected customers or websites. No backup data, credentials, or files are returned.",
                {
                    "scope": {"type": "string", "enum": ["all", "panel"]},
                    "customer_ids": {"type": "array", "items": {"type": "integer"}},
                    "site_ids": {"type": "array", "items": {"type": "integer"}},
                    "availability": {"type": "string", "enum": ["all", "available", "missing"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            ),
            cls._tool(
                "list_wordpress_users",
                "Read stored WordPress usernames and roles. Passwords, email addresses, and other credentials are never returned.",
                {
                    "scope": {"type": "string", "enum": ["all", "panel"]},
                    "customer_ids": {"type": "array", "items": {"type": "integer"}},
                    "site_ids": {"type": "array", "items": {"type": "integer"}},
                    "role": {"type": "string", "description": "A WordPress role such as administrator, or all."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            ),
            cls._tool(
                "set_site_selection",
                "Replace the side-panel selection with site IDs returned by query_sites. Use only when the user explicitly asks to select, choose, mark, or narrow websites.",
                {
                    "site_ids": {"type": "array", "items": {"type": "integer"}, "description": "Only IDs previously returned by query_sites."},
                },
            ),
        ]

    @staticmethod
    def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
            "strict": True,
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "search_customers": self._search_customers,
            "search_components": self._search_components,
            "query_sites": self._query_sites,
            "list_updates": self._list_updates,
            "list_backups": self._list_backups,
            "list_wordpress_users": self._list_wordpress_users,
            "set_site_selection": self._set_site_selection,
        }
        handler = handlers.get(name)
        if handler is None:
            raise AssistantToolError(f"Unknown assistant tool: {name}.")
        return handler(arguments)

    def _search_customers(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._required_text(arguments, "query")
        site_domains_by_customer: dict[int, list[str]] = {}
        for item in self.items:
            customer = item.site.customer
            if customer is not None:
                site_domains_by_customer.setdefault(customer.id, []).append(item.site.domain)

        matches: list[tuple[float, Customer]] = []
        for customer in self.db.scalars(select(Customer).order_by(Customer.name.asc(), Customer.id.asc())).all():
            values = [customer.name, customer.website_domain or "", *site_domains_by_customer.get(customer.id, [])]
            score = max((self._similarity(query, value) for value in values if value), default=0.0)
            if score >= 0.35:
                matches.append((score, customer))
        matches.sort(key=lambda item: (-item[0], item[1].name.casefold(), item[1].id))

        return {
            "query": query,
            "matches": [
                {
                    "id": customer.id,
                    "name": customer.name,
                    "zoho_status": customer.zoho_status or "",
                    "website_domain": customer.website_domain or "",
                    "linked_domains": sorted(site_domains_by_customer.get(customer.id, [])),
                    "match_score": round(score, 2),
                }
                for score, customer in matches[:MAX_SEARCH_RESULTS]
            ],
        }

    def _search_components(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._required_text(arguments, "query")
        kind = self._enum(arguments, "kind", {"all", "plugin", "theme"})
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in self.items:
            for component in self._components_for_item(item):
                if kind != "all" and component["kind"] != kind:
                    continue
                key = (component["kind"], component["identifier"])
                entry = grouped.setdefault(
                    key,
                    {
                        "kind": component["kind"],
                        "name": component["name"],
                        "identifier": component["identifier"],
                        "installed_site_count": 0,
                        "active_site_count": 0,
                    },
                )
                entry["installed_site_count"] += 1
                if component["active"] is True:
                    entry["active_site_count"] += 1

        matches = []
        for component in grouped.values():
            score = max(self._similarity(query, component["name"]), self._similarity(query, component["identifier"]))
            if score >= 0.35:
                matches.append((score, component))
        matches.sort(key=lambda item: (-item[0], item[1]["name"].casefold(), item[1]["identifier"]))
        return {
            "query": query,
            "matches": [
                {**component, "match_score": round(score, 2)}
                for score, component in matches[:MAX_SEARCH_RESULTS]
            ],
        }

    def _query_sites(self, arguments: dict[str, Any]) -> dict[str, Any]:
        items = self._scoped_items(arguments)
        component_kind = self._enum(arguments, "component_kind", {"all", "plugin", "theme"})
        component_identifier = self._text(arguments, "component_identifier")
        component_state = self._enum(arguments, "component_state", {"any", "installed", "active", "inactive"})
        update_state = self._enum(arguments, "update_state", {"any", "available", "not_available", "not_checked"})
        limit = self._limit(arguments)

        rows = []
        for item in items:
            matches = [
                component
                for component in self._components_for_item(item)
                if (component_kind == "all" or component["kind"] == component_kind)
                and (not component_identifier or component["identifier"] == component_identifier)
                and self._matches_component_state(component, component_state)
                and self._matches_update_state(item, component, update_state)
            ]
            if component_identifier or component_kind != "all" or component_state != "any":
                if not matches:
                    continue
            elif not self._matches_site_update_state(item, update_state):
                continue
            rows.append(self._site_row(item, matches))

        self._selection_candidates.update(row["id"] for row in rows)
        return self._limited_rows(rows, limit)

    def _list_updates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        items = self._scoped_items(arguments)
        kind = self._enum(arguments, "component_kind", {"all", "wordpress", "plugin", "theme"})
        component_identifier = self._text(arguments, "component_identifier")
        limit = self._limit(arguments)
        entries = self.inventory.build_update_workbench(items)
        rows = []
        for entry in entries:
            if not entry.update_available:
                continue
            if kind != "all" and entry.kind != kind:
                continue
            if component_identifier and entry.identifier != component_identifier:
                continue
            customer = entry.site.customer
            rows.append(
                {
                    "site_id": entry.site.id,
                    "domain": entry.site.domain,
                    "customer": self._customer_ref(customer),
                    "kind": entry.kind,
                    "name": entry.name,
                    "identifier": entry.identifier,
                    "current_version": entry.current_version,
                    "target_version": entry.target_version,
                    "direct_update_ready": entry.direct_update_selectable,
                    "checked_at": entry.captured_at.isoformat(),
                }
            )
        return self._limited_rows(rows, limit, key="updates")

    def _list_backups(self, arguments: dict[str, Any]) -> dict[str, Any]:
        items = self._scoped_items(arguments)
        availability = self._enum(arguments, "availability", {"all", "available", "missing"})
        limit = self._limit(arguments)
        snapshots = self.inventory.repository.get_latest_backup_snapshots_by_site_ids([item.site.id for item in items])
        rows = []
        for item in items:
            snapshot = snapshots.get(item.site.id)
            available = bool(snapshot and snapshot.backup_available)
            if availability == "available" and not available:
                continue
            if availability == "missing" and available:
                continue
            rows.append(
                {
                    "site_id": item.site.id,
                    "domain": item.site.domain,
                    "customer": self._customer_ref(item.site.customer),
                    "available": available,
                    "complete": bool(snapshot and snapshot.backup_complete),
                    "backup_at": snapshot.backup_at.isoformat() if snapshot and snapshot.backup_at else "",
                    "backup_count": snapshot.backup_count if snapshot else 0,
                    "checked_at": snapshot.captured_at.isoformat() if snapshot else "",
                }
            )
        return self._limited_rows(rows, limit, key="backups")

    def _list_wordpress_users(self, arguments: dict[str, Any]) -> dict[str, Any]:
        items = self._scoped_items(arguments)
        allowed_site_ids = {item.site.id for item in items}
        role = self._text(arguments, "role") or "all"
        limit = self._limit(arguments)
        rows = []
        for entry in SiteUserService(db=self.db, cipher=self.cipher).list_workbench_entries():
            if entry.site.id not in allowed_site_ids:
                continue
            if role != "all" and role not in entry.user["roles"]:
                continue
            rows.append(
                {
                    "site_id": entry.site.id,
                    "domain": entry.site.domain,
                    "customer": self._customer_ref(entry.site.customer),
                    "username": entry.user["username"],
                    "display_name": entry.user["display_name"],
                    "roles": entry.user["roles"],
                    "registered_at": entry.user["registered_at"],
                }
            )
        return self._limited_rows(rows, limit, key="users")

    def _set_site_selection(self, arguments: dict[str, Any]) -> dict[str, Any]:
        site_ids = self._positive_ids(arguments, "site_ids")
        unknown_ids = set(site_ids) - set(self.items_by_id)
        if unknown_ids:
            raise AssistantToolError("One or more selected site IDs are not registered in the Hub.")
        unsupported_ids = set(site_ids) - self._selection_candidates
        if unsupported_ids:
            raise AssistantToolError("Site selection must use IDs returned by query_sites in this assistant request.")
        self.state.selection_site_ids = tuple(sorted(set(site_ids)))
        return {
            "selected_site_ids": list(self.state.selection_site_ids),
            "selected_count": len(self.state.selection_site_ids),
            "domains": [self.items_by_id[site_id].site.domain for site_id in self.state.selection_site_ids],
        }

    def _scoped_items(self, arguments: dict[str, Any]) -> list[FleetInventoryItem]:
        scope = self._enum(arguments, "scope", {"all", "panel"})
        customer_ids = set(self._positive_ids(arguments, "customer_ids"))
        customer_status = self._enum_or_default(
            arguments,
            "customer_status",
            {"all", "Aktuell", "Neu", "gekündigt", "Kündigung liegt vor", "other", "unlinked"},
            default="all",
        )
        customer_name_prefix = self._text(arguments, "customer_name_prefix").casefold()
        site_ids = set(self._positive_ids(arguments, "site_ids"))
        items = self.items
        if scope == "panel":
            if self.state.panel_scope == "all":
                pass
            else:
                items = [item for item in items if item.site.id in self.state.panel_site_ids]
        if customer_ids:
            items = [item for item in items if item.site.customer_id in customer_ids]
        if customer_status == "unlinked":
            items = [item for item in items if item.site.customer is None]
        elif customer_status == "other":
            known_statuses = {"Aktuell", "Neu", "gekündigt", "Kündigung liegt vor"}
            items = [
                item
                for item in items
                if item.site.customer is not None and (item.site.customer.zoho_status or "") not in known_statuses
            ]
        elif customer_status != "all":
            items = [
                item
                for item in items
                if item.site.customer is not None and item.site.customer.zoho_status == customer_status
            ]
        if customer_name_prefix:
            items = [
                item
                for item in items
                if item.site.customer is not None and item.site.customer.name.casefold().startswith(customer_name_prefix)
            ]
        if site_ids:
            items = [item for item in items if item.site.id in site_ids]
        return items

    @staticmethod
    def _components_for_item(item: FleetInventoryItem) -> list[dict[str, Any]]:
        components = []
        for plugin in item.plugins:
            identifier = HubAssistantTools._value(plugin, "plugin_file")
            name = HubAssistantTools._value(plugin, "name") or identifier
            if identifier and name:
                components.append(
                    {
                        "kind": "plugin",
                        "identifier": identifier,
                        "name": name,
                        "version": HubAssistantTools._value(plugin, "version") or HubAssistantTools._value(plugin, "current_version"),
                        "active": plugin.get("active") if isinstance(plugin.get("active"), bool) else None,
                    }
                )
        themes = item.snapshot.themes_json if item.snapshot is not None and isinstance(item.snapshot.themes_json, list) else []
        for theme in themes:
            if not isinstance(theme, dict):
                continue
            identifier = HubAssistantTools._value(theme, "stylesheet") or HubAssistantTools._value(theme, "slug")
            name = HubAssistantTools._value(theme, "name") or identifier
            if identifier and name:
                components.append(
                    {
                        "kind": "theme",
                        "identifier": identifier,
                        "name": name,
                        "version": HubAssistantTools._value(theme, "version") or HubAssistantTools._value(theme, "current_version"),
                        "active": theme.get("active") if isinstance(theme.get("active"), bool) else None,
                    }
                )
        return components

    @staticmethod
    def _matches_component_state(component: dict[str, Any], state: str) -> bool:
        if state in {"any", "installed"}:
            return True
        return component["active"] is (state == "active")

    def _matches_update_state(self, item: FleetInventoryItem, component: dict[str, Any], state: str) -> bool:
        if state == "any":
            return True
        if item.update_snapshot is None:
            return state == "not_checked"
        updates = item.plugin_updates if component["kind"] == "plugin" else item.theme_updates
        key = "plugin_file" if component["kind"] == "plugin" else "stylesheet"
        has_update = any(self._value(update, key) == component["identifier"] for update in updates)
        return (state == "available" and has_update) or (state == "not_available" and not has_update)

    @staticmethod
    def _matches_site_update_state(item: FleetInventoryItem, state: str) -> bool:
        if state == "any":
            return True
        if item.update_snapshot is None:
            return state == "not_checked"
        has_update = item.update_count > 0
        return (state == "available" and has_update) or (state == "not_available" and not has_update)

    def _site_row(self, item: FleetInventoryItem, components: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": item.site.id,
            "domain": item.site.domain,
            "customer": self._customer_ref(item.site.customer),
            "site_status": item.site.status,
            "wordpress_version": item.site.wordpress_version or "",
            "bridge_version": item.site.bridge_version or "",
            "matched_components": components[:10],
            "state_checked_at": item.snapshot.captured_at.isoformat() if item.snapshot else "",
            "updates_checked_at": item.update_snapshot.captured_at.isoformat() if item.update_snapshot else "",
        }

    @staticmethod
    def _customer_ref(customer: Customer | None) -> dict[str, Any] | None:
        if customer is None:
            return None
        return {"id": customer.id, "name": customer.name, "zoho_status": customer.zoho_status or ""}

    @staticmethod
    def _limited_rows(rows: list[dict[str, Any]], limit: int, *, key: str = "sites") -> dict[str, Any]:
        return {key: rows[:limit], f"{key}_count": len(rows), "truncated": len(rows) > limit}

    @staticmethod
    def _value(value: object, key: str) -> str:
        if not isinstance(value, dict):
            return ""
        raw = value.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    @staticmethod
    def _canonical(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
        return re.sub(r"[^a-z0-9]+", " ", normalized).strip()

    @classmethod
    def _similarity(cls, query: str, value: str) -> float:
        left = cls._canonical(query)
        right = cls._canonical(value)
        if not left or not right:
            return 0.0
        compact_left = left.replace(" ", "")
        compact_right = right.replace(" ", "")
        if compact_left in compact_right or compact_right in compact_left:
            return 1.0
        sequence_score = SequenceMatcher(None, compact_left, compact_right).ratio()
        left_tokens = set(left.split())
        right_tokens = set(right.split())
        token_score = len(left_tokens & right_tokens) / max(1, len(left_tokens))
        return max(sequence_score, token_score)

    @staticmethod
    def _required_text(arguments: dict[str, Any], key: str) -> str:
        value = HubAssistantTools._text(arguments, key)
        if not value:
            raise AssistantToolError(f"{key} must be a non-empty string.")
        return value

    @staticmethod
    def _text(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key, "")
        if not isinstance(value, str):
            raise AssistantToolError(f"{key} must be a string.")
        return value.strip()

    @staticmethod
    def _enum(arguments: dict[str, Any], key: str, allowed: set[str]) -> str:
        value = HubAssistantTools._text(arguments, key)
        if value not in allowed:
            raise AssistantToolError(f"{key} has an unsupported value.")
        return value

    @staticmethod
    def _enum_or_default(arguments: dict[str, Any], key: str, allowed: set[str], *, default: str) -> str:
        if key not in arguments:
            return default
        return HubAssistantTools._enum(arguments, key, allowed)

    @staticmethod
    def _positive_ids(arguments: dict[str, Any], key: str) -> list[int]:
        value = arguments.get(key, [])
        if not isinstance(value, list):
            raise AssistantToolError(f"{key} must be an array.")
        result = []
        for raw in value:
            if isinstance(raw, bool):
                raise AssistantToolError(f"{key} must contain positive integer IDs.")
            try:
                identifier = int(raw)
            except (TypeError, ValueError) as exc:
                raise AssistantToolError(f"{key} must contain positive integer IDs.") from exc
            if identifier <= 0:
                raise AssistantToolError(f"{key} must contain positive integer IDs.")
            result.append(identifier)
        return result

    @staticmethod
    def _limit(arguments: dict[str, Any]) -> int:
        value = arguments.get("limit", MAX_TOOL_RESULTS)
        if isinstance(value, bool):
            raise AssistantToolError("limit must be an integer.")
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise AssistantToolError("limit must be an integer.") from exc
        if not 1 <= limit <= MAX_TOOL_RESULTS:
            raise AssistantToolError(f"limit must be between 1 and {MAX_TOOL_RESULTS}.")
        return limit
