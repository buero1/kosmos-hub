from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.hub_user import HubUser
from app.services.ai_provider import AiProviderConfigService
from app.services.hub_accounts import hash_password


def test_openai_key_is_encrypted_and_can_be_removed():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = HubUser(username="operator", password_hash=hash_password("correct-horse-battery-staple"), role="admin")
        db.add(user)
        db.commit()

        service = AiProviderConfigService(db=db, cipher=SecretCipher("a" * 32))
        config = service.configure_openai(actor=user, api_key="sk-test-abcdefghijklmnopqrstuvwxyz")
        db.commit()

        assert config.encrypted_api_key != "sk-test-abcdefghijklmnopqrstuvwxyz"
        stored_config, api_key = service.get_enabled_openai_api_key()
        assert stored_config.id == config.id
        assert api_key == "sk-test-abcdefghijklmnopqrstuvwxyz"

        service.remove_openai(actor=user)
        db.commit()
        assert service.get_openai_config() is None
