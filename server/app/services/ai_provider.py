from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.ai_provider_config import AiProviderConfig
from app.models.hub_user import HubUser

OPENAI_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"


class AiProviderConfigError(ValueError):
    pass


class AiProviderConfigService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def get_openai_config(self) -> AiProviderConfig | None:
        return self.db.scalar(select(AiProviderConfig).where(AiProviderConfig.provider == OPENAI_PROVIDER))

    def get_enabled_openai_api_key(self) -> tuple[AiProviderConfig, str]:
        config = self.get_openai_config()
        if config is None or not config.enabled:
            raise AiProviderConfigError("OpenAI is not configured in the Hub account settings.")
        try:
            return config, self.cipher.decrypt(config.encrypted_api_key)
        except Exception as exc:
            raise AiProviderConfigError("The stored OpenAI key could not be decrypted.") from exc

    def configure_openai(self, *, actor: HubUser, api_key: str) -> AiProviderConfig:
        if actor.role != "admin":
            raise AiProviderConfigError("Only Hub administrators can configure AI access.")

        normalized_key = self._normalize_api_key(api_key)
        config = self.get_openai_config()
        if config is None:
            config = AiProviderConfig(
                provider=OPENAI_PROVIDER,
                encrypted_api_key=self.cipher.encrypt(normalized_key),
                model=DEFAULT_OPENAI_MODEL,
                enabled=True,
                configured_by_user_id=actor.id,
            )
            self.db.add(config)
        else:
            config.encrypted_api_key = self.cipher.encrypt(normalized_key)
            config.model = DEFAULT_OPENAI_MODEL
            config.enabled = True
            config.configured_by_user_id = actor.id
            config.last_error = None

        self.db.flush()
        return config

    def remove_openai(self, *, actor: HubUser) -> None:
        if actor.role != "admin":
            raise AiProviderConfigError("Only Hub administrators can configure AI access.")
        config = self.get_openai_config()
        if config is None:
            raise AiProviderConfigError("No OpenAI configuration exists.")
        self.db.delete(config)

    def record_request_success(self, config: AiProviderConfig) -> None:
        config.last_used_at = datetime.now(UTC)
        config.last_error = None
        self.db.commit()

    def record_request_error(self, config: AiProviderConfig, *, code: str) -> None:
        config.last_error = code[:128]
        self.db.commit()

    @staticmethod
    def _normalize_api_key(api_key: str) -> str:
        normalized = api_key.strip()
        if len(normalized) < 20 or len(normalized) > 512 or any(character.isspace() for character in normalized):
            raise AiProviderConfigError("Enter a valid OpenAI API key without spaces.")
        if not normalized.startswith("sk-"):
            raise AiProviderConfigError("The OpenAI API key must start with sk-.")
        return normalized
