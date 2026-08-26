import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_user import HubUser
from app.models.provider_credential import ProviderCredential


class ProviderCredentialError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderCredentialRow:
    provider: str
    label: str
    configured_at: object
    last_used_at: object
    last_error: str | None
    automatic_use: bool


class ProviderCredentialService:
    """Stores provider secrets without exposing them back to the web UI."""

    _LABELS = {
        "crocoblock": "Crocoblock",
        "elementor": "Elementor Pro",
        "elementor-pro": "Elementor Pro",
    }

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher

    def list_rows(self) -> list[ProviderCredentialRow]:
        credentials = self.db.scalars(select(ProviderCredential).order_by(ProviderCredential.provider.asc())).all()
        return [
            ProviderCredentialRow(
                provider=credential.provider,
                label=self.provider_label(credential.provider),
                configured_at=credential.updated_at or credential.created_at,
                last_used_at=credential.last_used_at,
                last_error=credential.last_error,
                automatic_use=credential.provider == "crocoblock",
            )
            for credential in credentials
        ]

    def configure(self, *, actor: HubUser, provider: str, license_key: str) -> ProviderCredential:
        self._require_admin(actor)
        provider_key = self.normalize_provider(provider)
        normalized_key = self.normalize_license_key(license_key)
        credential = self.db.scalar(select(ProviderCredential).where(ProviderCredential.provider == provider_key))

        if credential is None:
            credential = ProviderCredential(
                provider=provider_key,
                encrypted_secret=self.cipher.encrypt(normalized_key),
                enabled=True,
                configured_by_user_id=actor.id,
            )
            self.db.add(credential)
        else:
            credential.encrypted_secret = self.cipher.encrypt(normalized_key)
            credential.enabled = True
            credential.configured_by_user_id = actor.id
            credential.last_error = None

        self.db.flush()
        return credential

    def remove(self, *, actor: HubUser, provider: str) -> ProviderCredential:
        self._require_admin(actor)
        provider_key = self.normalize_provider(provider)
        credential = self.db.scalar(select(ProviderCredential).where(ProviderCredential.provider == provider_key))
        if credential is None:
            raise ProviderCredentialError("This provider license is no longer stored in the Hub.")
        self.db.delete(credential)
        return credential

    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
        if not normalized or len(normalized) > 64:
            raise ProviderCredentialError("Enter a provider name with up to 64 letters, numbers, spaces, or hyphens.")
        return "elementor" if normalized == "elementor-pro" else normalized

    @staticmethod
    def normalize_license_key(value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8 or len(normalized) > 512 or any(character.isspace() for character in normalized):
            raise ProviderCredentialError("Enter a valid provider license key without spaces.")
        return normalized

    @classmethod
    def provider_label(cls, provider: str) -> str:
        return cls._LABELS.get(provider, provider.replace("-", " ").title())

    @staticmethod
    def _require_admin(actor: HubUser) -> None:
        if actor.role != "admin":
            raise ProviderCredentialError("Only Hub administrators can manage provider licenses.")
