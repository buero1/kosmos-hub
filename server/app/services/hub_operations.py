"""Shared operation registry and execution gateway for Hub interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
import json
import logging
from pkgutil import iter_modules
from typing import Callable, Mapping

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.mailbox_actor import with_mailbox_actor
from app.core.record_actor import with_record_actor
from app import services


class HubOperationError(ValueError):
    """A domain-safe error shared by human and agent callers."""


class HubOperationPending(HubOperationError):
    """A prerequisite is still running and the action may be retried."""


@dataclass(frozen=True)
class HubArtifact:
    filename: str
    content_type: str
    content: bytes
    recipient_email: str = ""


@dataclass(frozen=True)
class HubOperationResult:
    label: str
    href: str
    record_id: int
    background_token: str = ""
    outputs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HubOperationInputField:
    name: str
    label: str
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()
    multiple: bool = False
    context_type: str = ""
    max_length: int | None = None
    encoding: str = ""


@dataclass(frozen=True)
class HubOperation:
    key: str
    module: str
    label: str
    description: str
    input_guide: str
    preview_fields: tuple[tuple[str, str], ...]
    execute: Callable[["HubOperationService", Mapping[str, str]], HubOperationResult]
    preview_builder: Callable[[Mapping[str, str]], tuple[str, ...]] | None = None
    agent_enabled: bool = True
    defaults: Callable[[Mapping[str, str]], Mapping[str, str]] | None = None
    input_fields: Callable[[], tuple[HubOperationInputField, ...]] | None = None
    result_fields: tuple[tuple[str, str], ...] = ()

    def input_contract(self, values: Mapping[str, str] | None = None) -> dict[str, dict[str, object]]:
        defaults = self.defaults(values or {}) if self.defaults is not None else {}
        return self._input_contract(defaults)

    def _input_contract(self, defaults: Mapping[str, str]) -> dict[str, dict[str, object]]:
        contract: dict[str, dict[str, object]] = {}
        for definition in self.input_fields() if self.input_fields is not None else ():
            entry: dict[str, object] = {"label": definition.label, "required": definition.required}
            if definition.options:
                entry["options"] = dict(definition.options)
            if definition.multiple:
                entry["encoding"] = "comma-separated list"
            if definition.encoding:
                entry["encoding"] = definition.encoding
            if definition.context_type:
                entry["context_type"] = definition.context_type
            if definition.max_length is not None:
                entry["max_length"] = definition.max_length
            if definition.name in defaults:
                entry["default"] = defaults[definition.name]
            contract[definition.name] = entry
        return contract

    def agent_description(self) -> str:
        description = f"{self.key}: {self.description} Eingaben: {self.input_guide}"
        defaults = self.defaults({}) if self.defaults is not None else {}
        contract = self._input_contract(defaults)
        if contract:
            description += " Felddefinitionen aus dem Modulkatalog (JSON): " + json.dumps(contract, ensure_ascii=False)
        if self.defaults is not None:
            listed = ", ".join(f"{key}={value}" for key, value in defaults.items())
            description += f" Fehlende Eingaben erhalten automatisch die Masken-Standards: {listed}."
        description += " Ergebnisfelder für Folgeschritte (JSON): " + json.dumps(
            {"record_id": "ID des bearbeiteten Datensatzes", **dict(self.result_fields)}, ensure_ascii=False,
        )
        return description

    def apply_defaults(self, values: Mapping[str, str]) -> dict[str, str]:
        return {**self.defaults(values), **values} if self.defaults is not None else dict(values)

    def preview(self, values: Mapping[str, str]) -> tuple[str, ...]:
        if self.preview_builder is not None:
            return self.preview_builder(values)
        return tuple(f"{label}: {values[key]}" for key, label in self.preview_fields if values.get(key))


@dataclass(frozen=True)
class HubQuery:
    """Explicitly read-only entry point, never a mutation disguised as a tool."""

    key: str
    description: str
    input_fields: tuple[HubOperationInputField, ...]
    execute: Callable[["HubOperationService", Mapping[str, str]], dict[str, object]]

    def tool_definition(self) -> dict[str, object]:
        return {
            "type": "function", "name": self.key.replace(".", "__"), "strict": False,
            "description": self.description,
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {f.name: {"type": "string", "description": f.label,
                    **({"maxLength": f.max_length} if f.max_length is not None else {}),
                    **({"enum": [value for value, _ in f.options]} if f.options else {})}
                    for f in self.input_fields},
                "required": [f.name for f in self.input_fields if f.required],
            },
        }


_QUERIES: dict[str, HubQuery] = {}
_OPERATIONS: dict[str, HubOperation] = {}
_ARTIFACTS: dict[str, Callable[["HubOperationService", str], HubArtifact]] = {}
_loaded = False


def _load_operations() -> None:
    global _loaded
    if _loaded:
        return
    modules = sorted(
        module.name for module in iter_modules(services.__path__, f"{services.__name__}.")
        if module.name.startswith("app.services.hub_operation_")
    )
    for module in modules:
        import_module(module)
    _loaded = True


def register_operation(operation: HubOperation) -> HubOperation:
    if operation.key in _OPERATIONS:
        raise ValueError(f"Duplicate Hub operation: {operation.key}")
    _OPERATIONS[operation.key] = operation
    return operation


def register_artifact(kind: str, loader: Callable[["HubOperationService", str], HubArtifact]) -> None:
    if kind in _ARTIFACTS:
        raise ValueError(f"Duplicate Hub artifact: {kind}")
    _ARTIFACTS[kind] = loader


def register_query(query: HubQuery) -> HubQuery:
    if query.key in _QUERIES:
        raise ValueError(f"Duplicate Hub query: {query.key}")
    _QUERIES[query.key] = query
    return query


def hub_queries() -> tuple[HubQuery, ...]:
    _load_operations()
    return tuple(_QUERIES.values())


def get_operation(key: str) -> HubOperation | None:
    _load_operations()
    return _OPERATIONS.get(key)


def agent_operations() -> tuple[HubOperation, ...]:
    _load_operations()
    return tuple(operation for operation in _OPERATIONS.values() if operation.agent_enabled)


class HubOperationService:
    """Execute registered domain operations without duplicating route logic."""

    def __init__(self, *, db: Session, cipher: SecretCipher, actor: str, input_files: tuple[HubArtifact, ...] = ()):
        self.db = db
        self.cipher = cipher
        self.actor = actor
        self.input_files = input_files


    @with_mailbox_actor
    @with_record_actor
    def execute(self, key: str, values: Mapping[str, str]) -> HubOperationResult:
        operation = get_operation(key)
        if operation is None:
            raise HubOperationError("Diese Hub-Funktion ist nicht verfügbar.")
        return operation.execute(self, operation.apply_defaults(values))

    @with_mailbox_actor
    def query(self, key: str, values: Mapping[str, str], *, selection: dict | None = None) -> dict[str, object]:
        _load_operations()
        query = _QUERIES.get(key)
        if query is None:
            raise HubOperationError("Dieser Lesezugriff ist nicht verfuegbar.")
        fields = {f.name: f for f in query.input_fields}
        if not isinstance(values, dict) or set(values) - fields.keys():
            raise HubOperationError("Unbekannte Sucheingaben.")
        for name, definition in fields.items():
            value = values.get(name, "")
            if not isinstance(value, str) or len(value) > (definition.max_length or 255):
                raise HubOperationError("Ungueltige Sucheingabe.")
            if definition.required and not value.strip():
                raise HubOperationError(f"{definition.label} fehlt.")
            if value and definition.options and value not in dict(definition.options):
                raise HubOperationError("Ungueltige Auswahl fuer den Lesezugriff.")
        if selection is not None:
            from app.services.hub_query_selection import select_query
            return select_query(self, key, values, selection, definition=query)
        return query.execute(self, values)

    def after_commit(self, key: str, callback: Callable[[], None]) -> None:
        """Wake workers only after a successful outer commit, never on rollback."""
        if not self.db.info.get("hub_operation_commit_hooks"):
            self.db.info["hub_operation_commit_hooks"] = True

            def committed(session):
                transaction = session.get_nested_transaction() or session.get_transaction()
                pending = session.info.pop("hub_operation_callbacks", [])
                if transaction is not None and transaction.nested:
                    session.info["hub_operation_callbacks"] = [
                        (transaction.parent if tx is transaction else tx, name, fn)
                        for tx, name, fn in pending
                    ]
                else:
                    for fn in {name: fn for _tx, name, fn in pending}.values():
                        try:
                            fn()
                        except Exception:
                            logging.getLogger(__name__).exception("Hub post-commit notification failed")

            def ended(session, transaction):
                session.info["hub_operation_callbacks"] = [
                    entry for entry in session.info.get("hub_operation_callbacks", [])
                    if entry[0] is not transaction
                ]

            event.listen(self.db, "after_commit", committed)
            event.listen(self.db, "after_transaction_end", ended)
        transaction = self.db.get_nested_transaction() or self.db.get_transaction()
        self.db.info.setdefault("hub_operation_callbacks", []).append((transaction, key, callback))

    @with_mailbox_actor
    def load_artifact(self, reference: str) -> HubArtifact:
        _load_operations()
        kind, separator, identifier = reference.partition(":")
        loader = _ARTIFACTS.get(kind)
        if not separator or not identifier or loader is None:
            raise HubOperationError("Der angeforderte Anhang ist nicht verfügbar.")
        return loader(self, identifier)
