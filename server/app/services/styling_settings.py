from dataclasses import dataclass
import re

from sqlalchemy.orm import Session

from app.models.hub_user import HubUser
from app.models.styling_settings import StylingSettings


class StylingSettingsError(ValueError):
    pass


FONT_FAMILY_OPTIONS = (
    ("serif", "Klassisch Serif", 'Georgia, "Times New Roman", serif'),
    ("sans", "Klar Sans Serif", '"Trebuchet MS", "Segoe UI", sans-serif'),
    ("system", "Systemschrift", "system-ui, -apple-system, BlinkMacSystemFont, sans-serif"),
)
_FONT_FAMILY_BY_KEY = {key: css_value for key, _, css_value in FONT_FAMILY_OPTIONS}


@dataclass(frozen=True)
class StylingRuntimeSettings:
    font_family_key: str = "serif"
    font_family: str = 'Georgia, "Times New Roman", serif'
    background_color: str = "#f5f1e8"
    background_secondary_color: str = "#efe8da"
    panel_color: str = "#fffaf2"
    ink_color: str = "#1d2a2f"
    muted_color: str = "#6c7469"
    accent_color: str = "#0e7c66"
    accent_soft_color: str = "#d7efe8"
    border_color: str = "#d9d2c5"
    base_spacing: int = 16
    panel_radius: int = 18
    control_v1_height: int = 38
    control_v1_font_size: int = 13
    control_v1_padding: int = 12
    control_v1_radius: int = 5
    control_v2_height: int = 30
    control_v2_font_size: int = 12
    control_v2_padding: int = 10
    control_v2_radius: int = 3


class StylingSettingsService:
    """Stores validated, global CSS token values for the Hub UI."""

    _HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")

    def __init__(self, *, db: Session):
        self.db = db

    def get_runtime_settings(self) -> StylingRuntimeSettings:
        config = self.db.get(StylingSettings, 1)
        if config is None:
            return StylingRuntimeSettings()
        return StylingRuntimeSettings(
            font_family_key=config.font_family_key,
            font_family=_FONT_FAMILY_BY_KEY.get(config.font_family_key, StylingRuntimeSettings.font_family),
            background_color=config.background_color,
            background_secondary_color=config.background_secondary_color,
            panel_color=config.panel_color,
            ink_color=config.ink_color,
            muted_color=config.muted_color,
            accent_color=config.accent_color,
            accent_soft_color=config.accent_soft_color,
            border_color=config.border_color,
            base_spacing=config.base_spacing,
            panel_radius=config.panel_radius,
            control_v1_height=config.control_v1_height,
            control_v1_font_size=config.control_v1_font_size,
            control_v1_padding=config.control_v1_padding,
            control_v1_radius=config.control_v1_radius,
            control_v2_height=config.control_v2_height,
            control_v2_font_size=config.control_v2_font_size,
            control_v2_padding=config.control_v2_padding,
            control_v2_radius=config.control_v2_radius,
        )

    def configure(
        self,
        *,
        actor: HubUser,
        font_family_key: str,
        background_color: str,
        background_secondary_color: str,
        panel_color: str,
        ink_color: str,
        muted_color: str,
        accent_color: str,
        accent_soft_color: str,
        border_color: str,
        base_spacing: int,
        panel_radius: int,
        control_v1_height: int,
        control_v1_font_size: int,
        control_v1_padding: int,
        control_v1_radius: int,
        control_v2_height: int,
        control_v2_font_size: int,
        control_v2_padding: int,
        control_v2_radius: int,
    ) -> StylingSettings:
        if actor.role != "admin":
            raise StylingSettingsError("Only Hub administrators can change global styling.")
        self._validate(
            font_family_key=font_family_key,
            colors=(background_color, background_secondary_color, panel_color, ink_color, muted_color, accent_color, accent_soft_color, border_color),
            base_spacing=base_spacing,
            panel_radius=panel_radius,
            variants=(
                (control_v1_height, control_v1_font_size, control_v1_padding, control_v1_radius),
                (control_v2_height, control_v2_font_size, control_v2_padding, control_v2_radius),
            ),
        )

        config = self.db.get(StylingSettings, 1)
        if config is None:
            config = StylingSettings(id=1)
            self.db.add(config)
        config.font_family_key = font_family_key
        config.background_color = background_color.lower()
        config.background_secondary_color = background_secondary_color.lower()
        config.panel_color = panel_color.lower()
        config.ink_color = ink_color.lower()
        config.muted_color = muted_color.lower()
        config.accent_color = accent_color.lower()
        config.accent_soft_color = accent_soft_color.lower()
        config.border_color = border_color.lower()
        config.base_spacing = base_spacing
        config.panel_radius = panel_radius
        config.control_v1_height = control_v1_height
        config.control_v1_font_size = control_v1_font_size
        config.control_v1_padding = control_v1_padding
        config.control_v1_radius = control_v1_radius
        config.control_v2_height = control_v2_height
        config.control_v2_font_size = control_v2_font_size
        config.control_v2_padding = control_v2_padding
        config.control_v2_radius = control_v2_radius
        config.configured_by_user_id = actor.id
        self.db.flush()
        return config

    @classmethod
    def _validate(
        cls,
        *,
        font_family_key: str,
        colors: tuple[str, ...],
        base_spacing: int,
        panel_radius: int,
        variants: tuple[tuple[int, int, int, int], ...],
    ) -> None:
        if font_family_key not in _FONT_FAMILY_BY_KEY:
            raise StylingSettingsError("Choose a supported base font.")
        if not all(cls._HEX_COLOR.fullmatch(color or "") for color in colors):
            raise StylingSettingsError("Colors must use the #RRGGBB format.")
        cls._validate_range("Base spacing", base_spacing, minimum=4, maximum=40)
        cls._validate_range("Panel radius", panel_radius, minimum=0, maximum=40)
        for index, (height, font_size, padding, radius) in enumerate(variants, start=1):
            cls._validate_range(f"Variant {index} height", height, minimum=24, maximum=72)
            cls._validate_range(f"Variant {index} font size", font_size, minimum=10, maximum=24)
            cls._validate_range(f"Variant {index} padding", padding, minimum=0, maximum=32)
            cls._validate_range(f"Variant {index} radius", radius, minimum=0, maximum=32)

    @staticmethod
    def _validate_range(label: str, value: int, *, minimum: int, maximum: int) -> None:
        if not minimum <= value <= maximum:
            raise StylingSettingsError(f"{label} must be between {minimum} and {maximum}px.")
