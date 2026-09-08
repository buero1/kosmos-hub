"""Hub-wide defaults for the rich-text email composer."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.email_composer_settings import EmailComposerSettings


FONT_FAMILY_OPTIONS = (
    ("arial", "Arial", "Arial, Helvetica, sans-serif"),
    ("verdana", "Verdana", "Verdana, Geneva, sans-serif"),
    ("georgia", "Georgia", "Georgia, Times New Roman, serif"),
    ("courier", "Courier New", "Courier New, Courier, monospace"),
)
FONT_SIZE_OPTIONS = (12, 14, 16, 18)
LINE_HEIGHT_OPTIONS = (1.0, 1.1, 1.2, 1.3, 1.4, 1.5)
_FONT_FAMILIES = {key: css_value for key, _label, css_value in FONT_FAMILY_OPTIONS}
DEFAULT_FONT_FAMILY_KEY = "verdana"
DEFAULT_FONT_FAMILY = _FONT_FAMILIES[DEFAULT_FONT_FAMILY_KEY]
DEFAULT_FONT_SIZE = 12
DEFAULT_LINE_HEIGHT = 1.1


class EmailComposerSettingsError(ValueError):
    """Raised for an invalid editor preference submitted by an administrator."""


@dataclass(frozen=True)
class EmailComposerRuntimeSettings:
    font_family_key: str = DEFAULT_FONT_FAMILY_KEY
    font_family: str = DEFAULT_FONT_FAMILY
    font_size: int = DEFAULT_FONT_SIZE
    line_height: float = DEFAULT_LINE_HEIGHT
    signature_html: str = ""

    @property
    def font_style(self) -> str:
        return (
            f"font-family: {self.font_family}; font-size: {self.font_size}px; "
            f"line-height: {self.line_height:g}"
        )


class EmailComposerSettingsService:
    """Read and update the singleton settings record without exposing raw CSS input."""

    def __init__(self, *, db: Session) -> None:
        self.db = db

    def get_runtime_settings(self) -> EmailComposerRuntimeSettings:
        stored = self.db.get(EmailComposerSettings, 1)
        if stored is None:
            return EmailComposerRuntimeSettings()
        family_key = stored.font_family_key if stored.font_family_key in _FONT_FAMILIES else DEFAULT_FONT_FAMILY_KEY
        font_size = stored.font_size if stored.font_size in FONT_SIZE_OPTIONS else DEFAULT_FONT_SIZE
        line_height = stored.line_height if stored.line_height in LINE_HEIGHT_OPTIONS else DEFAULT_LINE_HEIGHT
        return EmailComposerRuntimeSettings(
            font_family_key=family_key,
            font_family=_FONT_FAMILIES[family_key],
            font_size=font_size,
            line_height=line_height,
            signature_html=stored.signature_html or "",
        )

    def configure(
        self,
        *,
        font_family_key: str,
        font_size: int,
        line_height: float,
    ) -> EmailComposerRuntimeSettings:
        family_key = font_family_key.strip().casefold()
        if family_key not in _FONT_FAMILIES:
            raise EmailComposerSettingsError("Wähle eine gültige Standardschrift aus.")
        if font_size not in FONT_SIZE_OPTIONS:
            raise EmailComposerSettingsError("Wähle eine gültige Standardschriftgröße aus.")
        if line_height not in LINE_HEIGHT_OPTIONS:
            raise EmailComposerSettingsError("Wähle eine gültige Standardzeilenhöhe aus.")

        stored = self._stored_settings()
        stored.font_family_key = family_key
        stored.font_size = font_size
        stored.line_height = line_height
        self.db.flush()
        return self.get_runtime_settings()

    def configure_signature(self, *, signature_html: str) -> EmailComposerRuntimeSettings:
        """Persist the already sanitized, Hub-wide signature used by template placeholders."""
        if len(signature_html) > 50_000:
            raise EmailComposerSettingsError("Die Signatur darf höchstens 50.000 Zeichen enthalten.")
        stored = self._stored_settings()
        stored.signature_html = signature_html
        self.db.flush()
        return self.get_runtime_settings()

    def apply_default_style(self, content: str) -> str:
        """Keep the configured default in sent HTML while allowing child styles to override it."""
        settings = self.get_runtime_settings()
        return f'<div style="{settings.font_style}">{content}</div>'

    def _stored_settings(self) -> EmailComposerSettings:
        stored = self.db.get(EmailComposerSettings, 1)
        if stored is None:
            stored = EmailComposerSettings(id=1)
            self.db.add(stored)
        return stored
