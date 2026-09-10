"""Persisted shortcuts for common AI email-editing instructions."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.email_ai_prompt_preset import EmailAiPromptPreset
from app.models.hub_user import HubUser


class EmailAiPromptPresetError(ValueError):
    pass


@dataclass(frozen=True)
class EmailAiPromptPresetInput:
    preset_id: int
    label: str
    instruction: str
    is_enabled: bool


@dataclass(frozen=True)
class EmailAiPromptPresetDefinition:
    label: str
    instruction: str


DEFAULT_EMAIL_AI_PROMPT_PRESETS = (
    EmailAiPromptPresetDefinition(
        label="Inhaltlich prüfen und verbessern",
        instruction="Inhaltlich prüfen und verbessern",
    ),
    EmailAiPromptPresetDefinition(
        label="Sprache korrigieren",
        instruction="Sprache korrigieren",
    ),
    EmailAiPromptPresetDefinition(
        label="Kürzer formulieren",
        instruction="Kürzer formulieren",
    ),
    EmailAiPromptPresetDefinition(
        label="Klarer und verständlicher formulieren",
        instruction="Klarer und verständlicher formulieren",
    ),
)


class EmailAiPromptPresetService:
    """Keeps email AI shortcut labels and their user instructions editable."""

    def __init__(self, *, db: Session):
        self.db = db

    def ensure_default_presets(self) -> None:
        if self.db.scalar(select(EmailAiPromptPreset.id).limit(1)) is not None:
            return
        self.db.add_all(
            EmailAiPromptPreset(
                label=preset.label,
                instruction=preset.instruction,
                sort_order=index,
            )
            for index, preset in enumerate(DEFAULT_EMAIL_AI_PROMPT_PRESETS, start=1)
        )
        self.db.flush()

    def list_presets(self, *, enabled_only: bool = False) -> tuple[EmailAiPromptPreset, ...]:
        self.ensure_default_presets()
        statement = select(EmailAiPromptPreset).order_by(
            EmailAiPromptPreset.sort_order.asc(),
            EmailAiPromptPreset.id.asc(),
        )
        if enabled_only:
            statement = statement.where(EmailAiPromptPreset.is_enabled.is_(True))
        return tuple(self.db.scalars(statement).all())

    def update_presets(self, *, actor: HubUser, presets: tuple[EmailAiPromptPresetInput, ...]) -> None:
        self._require_admin(actor)
        if not presets:
            return
        stored_by_id = {preset.id: preset for preset in self.list_presets()}
        incoming_ids = {preset.preset_id for preset in presets}
        if len(incoming_ids) != len(presets) or not incoming_ids.issubset(stored_by_id):
            raise EmailAiPromptPresetError("Die Schnellaktionen haben sich geändert. Bitte lade die Seite neu.")
        for preset_input in presets:
            label, instruction = self._validated_values(
                label=preset_input.label,
                instruction=preset_input.instruction,
            )
            stored = stored_by_id[preset_input.preset_id]
            stored.label = label
            stored.instruction = instruction
            stored.is_enabled = preset_input.is_enabled
        self.db.flush()

    def add_preset(self, *, actor: HubUser, label: str, instruction: str) -> EmailAiPromptPreset:
        self._require_admin(actor)
        label, instruction = self._validated_values(label=label, instruction=instruction)
        next_sort_order = int(self.db.scalar(select(func.max(EmailAiPromptPreset.sort_order))) or 0) + 1
        preset = EmailAiPromptPreset(
            label=label,
            instruction=instruction,
            sort_order=next_sort_order,
        )
        self.db.add(preset)
        self.db.flush()
        return preset

    @staticmethod
    def _require_admin(actor: HubUser) -> None:
        if actor.role != "admin":
            raise EmailAiPromptPresetError("Nur Hub-Administratoren können E-Mail-Schnellaktionen ändern.")

    @staticmethod
    def _validated_values(*, label: str, instruction: str) -> tuple[str, str]:
        normalized_label = label.strip()
        normalized_instruction = instruction.strip()
        if not 3 <= len(normalized_label) <= 120:
            raise EmailAiPromptPresetError("Die Bezeichnung muss zwischen 3 und 120 Zeichen lang sein.")
        if not 3 <= len(normalized_instruction) <= 1_000:
            raise EmailAiPromptPresetError("Die Anweisung muss zwischen 3 und 1.000 Zeichen lang sein.")
        return normalized_label, normalized_instruction
