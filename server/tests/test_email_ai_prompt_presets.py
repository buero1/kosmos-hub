import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.email_ai_prompt_presets import (
    DEFAULT_EMAIL_AI_PROMPT_PRESETS,
    EmailAiPromptPresetError,
    EmailAiPromptPresetInput,
    EmailAiPromptPresetService,
)
from app.services.hub_accounts import hash_password


def test_email_ai_prompt_presets_seed_in_order_and_remain_editable():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="admin", password_hash=hash_password("correct-horse-battery-staple"), role="admin")
        viewer = HubUser(username="viewer", password_hash=hash_password("correct-horse-battery-staple"), role="viewer")
        db.add_all((admin, viewer))
        db.commit()

        service = EmailAiPromptPresetService(db=db)
        presets = service.list_presets()

        assert tuple(preset.label for preset in presets) == tuple(preset.label for preset in DEFAULT_EMAIL_AI_PROMPT_PRESETS)
        assert tuple(preset.instruction for preset in presets) == tuple(preset.instruction for preset in DEFAULT_EMAIL_AI_PROMPT_PRESETS)

        service.update_presets(
            actor=admin,
            presets=tuple(
                EmailAiPromptPresetInput(
                    preset_id=preset.id,
                    label="Freundlicher formulieren" if index == 1 else preset.label,
                    instruction="Formuliere die E-Mail freundlicher." if index == 1 else preset.instruction,
                    is_enabled=index != 2,
                )
                for index, preset in enumerate(presets)
            ),
        )
        service.add_preset(
            actor=admin,
            label="In Stichpunkte umwandeln",
            instruction="Wandle die E-Mail in übersichtliche Stichpunkte um.",
        )

        enabled = service.list_presets(enabled_only=True)
        assert tuple(preset.label for preset in enabled) == (
            "Inhaltlich prüfen und verbessern",
            "Freundlicher formulieren",
            "Klarer und verständlicher formulieren",
            "In Stichpunkte umwandeln",
        )
        assert enabled[1].instruction == "Formuliere die E-Mail freundlicher."

        with pytest.raises(EmailAiPromptPresetError, match="Administratoren"):
            service.add_preset(actor=viewer, label="Test Auswahl", instruction="Eine gültige Anweisung")
