from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.hub_user import HubUser
from app.models.provider_credential import ProviderCredential
from app.models.site import SiteStatus
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.site_updates import SiteUpdateService


CROCOBLOCK_PROVIDER = "crocoblock"
CROCOBLOCK_ACTIVATE_ABILITY = "kosmos-bridge/activate-crocoblock-license"


class CrocoblockLicenseError(ValueError):
    pass


class CrocoblockLicenseService:
    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.cipher = cipher
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def get_config(self) -> ProviderCredential | None:
        return self.db.scalar(select(ProviderCredential).where(ProviderCredential.provider == CROCOBLOCK_PROVIDER))

    def configure(self, *, actor: HubUser, license_key: str) -> ProviderCredential:
        self._require_admin(actor)
        normalized_key = self._normalize_license_key(license_key)
        config = self.get_config()
        if config is None:
            config = ProviderCredential(
                provider=CROCOBLOCK_PROVIDER,
                encrypted_secret=self.cipher.encrypt(normalized_key),
                enabled=True,
                configured_by_user_id=actor.id,
            )
            self.db.add(config)
        else:
            config.encrypted_secret = self.cipher.encrypt(normalized_key)
            config.enabled = True
            config.configured_by_user_id = actor.id
            config.last_error = None

        self.db.flush()
        return config

    def remove(self, *, actor: HubUser) -> None:
        self._require_admin(actor)
        config = self.get_config()
        if config is None:
            raise CrocoblockLicenseError("No Crocoblock license is stored in the Hub.")
        self.db.delete(config)

    def activate_for_site(self, *, actor: HubUser, site_id: int) -> dict[str, object]:
        self._require_admin(actor)
        config = self.get_config()
        if config is None or not config.enabled:
            raise CrocoblockLicenseError("Save a Crocoblock license in the Hub before activating a site.")

        site = self.repository.get_site(site_id)
        if site is None:
            raise CrocoblockLicenseError("The selected site no longer exists.")
        if site.status != SiteStatus.verified.value:
            raise CrocoblockLicenseError("Only verified sites can receive a Crocoblock license activation.")

        try:
            license_key = self.cipher.decrypt(config.encrypted_secret)
        except Exception as exc:
            config.last_error = "credential_decryption_failed"
            self.db.commit()
            raise CrocoblockLicenseError("The stored Crocoblock license could not be decrypted.") from exc

        try:
            payload = self.proxy.execute_ability(
                site_id,
                CROCOBLOCK_ACTIVATE_ABILITY,
                {"license_key": license_key},
                timeout_seconds=75,
            )
        except SiteMcpProxyError as exc:
            config.last_error = exc.code[:128]
            write_audit_log(
                self.db,
                site=site,
                actor=actor.username,
                source="hub-web",
                action="activate-crocoblock-license",
                result="failed",
                detail=f"Crocoblock license activation could not run for {site.domain}: {exc.code}.",
            )
            self.db.commit()
            raise CrocoblockLicenseError(exc.message) from exc

        result = payload.get("result")
        if not isinstance(result, dict) or result.get("activated") is not True or result.get("site_activated") is not True:
            config.last_error = "activation_not_verified"
            write_audit_log(
                self.db,
                site=site,
                actor=actor.username,
                source="hub-web",
                action="activate-crocoblock-license",
                result="failed",
                detail=f"Crocoblock did not verify license activation for {site.domain}.",
            )
            self.db.commit()
            raise CrocoblockLicenseError("Crocoblock did not verify this license activation for the selected site.")

        config.last_used_at = datetime.now(UTC)
        config.last_error = None
        write_audit_log(
            self.db,
            site=site,
            actor=actor.username,
            source="hub-web",
            action="activate-crocoblock-license",
            result="success",
            detail=f"Activated the centrally stored Crocoblock license for {site.domain}. The license key was not logged.",
        )
        self.db.commit()

        updates_refreshed = True
        try:
            SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(site_id)
        except SiteMcpProxyError:
            updates_refreshed = False

        return {"site": site, "updates_refreshed": updates_refreshed}

    def _require_admin(self, actor: HubUser) -> None:
        if actor.role != "admin":
            raise CrocoblockLicenseError("Only Hub administrators can manage provider licenses.")

    @staticmethod
    def _normalize_license_key(value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8 or len(normalized) > 512 or any(character.isspace() for character in normalized):
            raise CrocoblockLicenseError("Enter a valid Crocoblock license key without spaces.")
        return normalized
