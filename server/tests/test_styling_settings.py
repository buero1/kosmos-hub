import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.styling_settings import StylingSettingsError, StylingSettingsService


def test_styling_settings_default_to_the_two_requested_control_variants():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        settings = StylingSettingsService(db=db).get_runtime_settings()

    assert (settings.control_v1_height, settings.control_v1_font_size, settings.control_v1_padding, settings.control_v1_radius) == (38, 13, 12, 5)
    assert (settings.control_v2_height, settings.control_v2_font_size, settings.control_v2_padding, settings.control_v2_radius) == (30, 12, 10, 3)


def test_styling_settings_are_persisted_after_an_admin_changes_them():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.commit()
        service = StylingSettingsService(db=db)

        service.configure(
            actor=admin,
            font_family_key="system",
            background_color="#ffffff",
            background_secondary_color="#f0f0f0",
            panel_color="#fafafa",
            ink_color="#102030",
            muted_color="#506070",
            accent_color="#008866",
            accent_soft_color="#d0f0e8",
            border_color="#c0c8d0",
            base_spacing=20,
            panel_radius=12,
            control_v1_height=40,
            control_v1_font_size=14,
            control_v1_padding=13,
            control_v1_radius=6,
            control_v2_height=32,
            control_v2_font_size=12,
            control_v2_padding=9,
            control_v2_radius=2,
        )
        db.commit()
        settings = service.get_runtime_settings()

    assert settings.font_family_key == "system"
    assert settings.font_family.startswith("system-ui")
    assert settings.accent_color == "#008866"
    assert settings.background_secondary_color == "#f0f0f0"
    assert settings.control_v1_height == 40
    assert settings.control_v2_radius == 2


def test_styling_settings_reject_non_admin_users():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="viewer", password_hash="hashed", role="viewer")
        db.add(user)
        db.commit()
        service = StylingSettingsService(db=db)

        with pytest.raises(StylingSettingsError, match="administrators"):
            service.configure(
                actor=user,
                font_family_key="serif",
                background_color="#f5f1e8",
                background_secondary_color="#efe8da",
                panel_color="#fffaf2",
                ink_color="#1d2a2f",
                muted_color="#6c7469",
                accent_color="#0e7c66",
                accent_soft_color="#d7efe8",
                border_color="#d9d2c5",
                base_spacing=16,
                panel_radius=18,
                control_v1_height=38,
                control_v1_font_size=13,
                control_v1_padding=12,
                control_v1_radius=5,
                control_v2_height=30,
                control_v2_font_size=12,
                control_v2_padding=10,
                control_v2_radius=3,
            )
