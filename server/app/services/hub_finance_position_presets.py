"""Named, reusable position packages for Finance documents."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_finance_article import HubFinanceArticle
from app.models.hub_finance_position_preset import HubFinancePositionPreset


SALES_POSITION_PRESET_LIBRARY = "sales"
INVOICE_POSITION_PRESET_LIBRARY = "invoices"
POSITION_PRESET_LIBRARIES = {
    SALES_POSITION_PRESET_LIBRARY: "Angebote & Aufträge",
    INVOICE_POSITION_PRESET_LIBRARY: "Rechnungen",
}
_CENT = Decimal("0.01")
_QUANTITY_STEP = Decimal("0.01")
_LINE_FIELDS = (
    "article_id",
    "name",
    "sku",
    "description",
    "quantity",
    "unit",
    "unit_price",
    "discount_percent",
    "tax_rate",
)


class HubFinancePositionPresetError(ValueError):
    """A safe validation error for the position package library."""


@dataclass(frozen=True)
class FinancePositionPresetView:
    id: int
    library_key: str
    name: str
    lines: tuple[dict[str, str], ...]
    created_by_username: str

    @property
    def line_count(self) -> int:
        return len(self.lines)


class HubFinancePositionPresetService:
    """Store encrypted line-item snapshots in two deliberately separate libraries."""

    _MAX_TEXT_LENGTH = 20_000
    _MAX_LINES = 250

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_presets(self, *, library_key: str) -> tuple[FinancePositionPresetView, ...]:
        library = self._library(library_key)
        presets = self.db.scalars(
            select(HubFinancePositionPreset)
            .where(HubFinancePositionPreset.library_key == library)
            .order_by(HubFinancePositionPreset.name.asc(), HubFinancePositionPreset.id.asc())
        ).all()
        return tuple(self._view(preset) for preset in presets)

    def get(self, *, preset_id: int, library_key: str) -> FinancePositionPresetView:
        library = self._library(library_key)
        preset = self.db.get(HubFinancePositionPreset, preset_id)
        if preset is None or preset.library_key != library:
            raise HubFinancePositionPresetError("Das Positionspaket wurde nicht gefunden.")
        return self._view(preset)

    def create(
        self,
        *,
        library_key: str,
        name: str,
        lines: object,
        actor_username: str,
    ) -> FinancePositionPresetView:
        library = self._library(library_key)
        display_name = self._name(name)
        validated_lines = self._validated_lines(lines)
        preset = HubFinancePositionPreset(
            library_key=library,
            name=display_name,
            normalized_name=display_name.casefold(),
            encrypted_lines_json=self.cipher.encrypt(
                json.dumps(validated_lines, ensure_ascii=False, separators=(",", ":"))
            ),
            created_by_username=self._limited_text(actor_username, "Benutzer") or "system",
        )
        self.db.add(preset)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise HubFinancePositionPresetError(
                "In diesem Ordner gibt es bereits ein Positionspaket mit diesem Namen."
            ) from exc
        return self._view(preset)

    def delete(self, *, preset_id: int, library_key: str) -> HubFinancePositionPreset:
        library = self._library(library_key)
        preset = self.db.get(HubFinancePositionPreset, preset_id)
        if preset is None or preset.library_key != library:
            raise HubFinancePositionPresetError("Das Positionspaket wurde nicht gefunden.")
        self.db.delete(preset)
        self.db.flush()
        return preset

    def _view(self, preset: HubFinancePositionPreset) -> FinancePositionPresetView:
        try:
            raw_lines = json.loads(self.cipher.decrypt(preset.encrypted_lines_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HubFinancePositionPresetError(
                f'Das Positionspaket "{preset.name}" enthält ungültige Daten.'
            ) from exc
        lines = self._validated_lines(raw_lines)
        linked_article_ids = tuple(
            int(line["article_id"])
            for line in lines
            if line["article_id"].isdigit()
        )
        existing_article_ids = (
            set(
                self.db.scalars(
                    select(HubFinanceArticle.id).where(
                        HubFinanceArticle.id.in_(linked_article_ids)
                    )
                ).all()
            )
            if linked_article_ids
            else set()
        )
        safe_lines = tuple(
            {
                **line,
                "article_id": line["article_id"] if int(line["article_id"] or 0) in existing_article_ids else "",
            }
            for line in lines
        )
        return FinancePositionPresetView(
            id=preset.id,
            library_key=preset.library_key,
            name=preset.name,
            lines=safe_lines,
            created_by_username=preset.created_by_username,
        )

    def _validated_lines(self, raw_lines: object) -> tuple[dict[str, str], ...]:
        if not isinstance(raw_lines, (list, tuple)):
            raise HubFinancePositionPresetError("Die Positionen konnten nicht gelesen werden.")
        if not raw_lines:
            raise HubFinancePositionPresetError("Ein Positionspaket benötigt mindestens eine Position.")
        if len(raw_lines) > self._MAX_LINES:
            raise HubFinancePositionPresetError(
                f"Ein Positionspaket darf höchstens {self._MAX_LINES} Positionen enthalten."
            )
        lines = []
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise HubFinancePositionPresetError("Eine gespeicherte Position ist ungültig.")
            line = {field: self._limited_text(raw.get(field), "Position") for field in _LINE_FIELDS}
            if not line["name"]:
                raise HubFinancePositionPresetError("Jede gespeicherte Position benötigt eine Bezeichnung.")
            if line["article_id"]:
                try:
                    article_id = int(line["article_id"])
                except ValueError as exc:
                    raise HubFinancePositionPresetError("Eine Artikelverknüpfung ist ungültig.") from exc
                if article_id <= 0:
                    raise HubFinancePositionPresetError("Eine Artikelverknüpfung ist ungültig.")
                line["article_id"] = str(article_id)
            line["quantity"] = self._decimal_text(
                line["quantity"],
                "Menge",
                minimum=Decimal("0.01"),
                places=_QUANTITY_STEP,
            )
            line["unit_price"] = self._decimal_text(
                line["unit_price"],
                "Tarif",
                minimum=Decimal("0"),
                places=_CENT,
            )
            line["discount_percent"] = self._decimal_text(
                line["discount_percent"] or "0",
                "Rabatt",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
                places=_CENT,
            )
            if line["tax_rate"] not in {"0", "7", "19"}:
                raise HubFinancePositionPresetError("Der USt.-Satz einer Position ist ungültig.")
            lines.append(line)
        return tuple(lines)

    @staticmethod
    def _library(value: str) -> str:
        if value not in POSITION_PRESET_LIBRARIES:
            raise HubFinancePositionPresetError("Der Positionspaket-Ordner wurde nicht gefunden.")
        return value

    @staticmethod
    def _name(value: str) -> str:
        normalized = " ".join(str(value or "").split())
        if not 3 <= len(normalized) <= 255:
            raise HubFinancePositionPresetError("Der Name muss zwischen 3 und 255 Zeichen lang sein.")
        return normalized

    @classmethod
    def _limited_text(cls, value: object, label: str) -> str:
        text = str(value or "").strip()
        if len(text) > cls._MAX_TEXT_LENGTH:
            raise HubFinancePositionPresetError(f"{label} ist zu lang.")
        return text

    @staticmethod
    def _decimal_text(
        value: str,
        label: str,
        *,
        minimum: Decimal,
        places: Decimal,
        maximum: Decimal | None = None,
    ) -> str:
        normalized = value.replace(" ", "").replace(",", ".")
        try:
            decimal_value = Decimal(normalized)
            if not decimal_value.is_finite() or decimal_value < minimum or (maximum is not None and decimal_value > maximum):
                raise HubFinancePositionPresetError(f"{label} ist ungültig.")
            return format(decimal_value.quantize(places, rounding=ROUND_HALF_UP), "f")
        except InvalidOperation as exc:
            raise HubFinancePositionPresetError(f"{label} ist ungültig.") from exc
