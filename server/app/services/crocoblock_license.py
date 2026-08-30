from datetime import UTC, datetime
from typing import Any, Callable

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
CROCOBLOCK_DASHBOARD_DIAGNOSTIC_ABILITY = "kosmos-bridge/diagnose-crocoblock-dashboard"
CROCOBLOCK_DASHBOARD_UNAVAILABLE_CODE = "KOSMOS_BRIDGE_CROCOBLOCK_DASHBOARD_UNAVAILABLE"


class CrocoblockLicenseError(ValueError):
    def __init__(self, message: str, *, code: str = "", dashboard_diagnostic: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.dashboard_diagnostic = dashboard_diagnostic or {}


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

    def activate_for_plugin_update(self, *, actor: str, site_id: int) -> dict[str, object]:
        """Make a stored license available for one pending Jet plugin update."""
        try:
            return self._activate_for_site(actor=actor, site_id=site_id, purpose="plugin update")
        except CrocoblockLicenseError as exc:
            if exc.code != CROCOBLOCK_DASHBOARD_UNAVAILABLE_CODE:
                raise
            dashboard_diagnostic = self._diagnose_crocoblock_dashboard(site_id)
            raise CrocoblockLicenseError(
                self._activation_failure_detail(exc, dashboard_diagnostic),
                code=exc.code,
                dashboard_diagnostic=dashboard_diagnostic,
            ) from exc

    def refresh_version_evidence(
        self,
        *,
        actor: str,
        site_ids: set[int],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        site_result_callback: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Authorize Jet Dashboard metadata checks without installing any plugin."""
        eligible_site_ids = sorted(site_ids)
        config = self.get_config()
        summary = {
            "eligible": len(eligible_site_ids),
            "completed": 0,
            "activated": 0,
            "refreshed": 0,
            "failed": 0,
            "skipped": 0,
            "license_available": int(config is not None and config.enabled),
            "versions": [],
        }
        if config is None or not config.enabled:
            summary["skipped"] = len(eligible_site_ids)
            summary["completed"] = len(eligible_site_ids)
            if site_result_callback is not None:
                for site_id in eligible_site_ids:
                    site_result_callback(
                        {
                            "site_id": site_id,
                            "status": "license-unavailable",
                            "detail": "No enabled Crocoblock license is stored in the Hub.",
                            "license_was_already_active": None,
                            "update_package_ready": None,
                            "provider_versions": [],
                        }
                    )
            if progress_callback is not None:
                progress_callback(summary.copy())
            return summary

        if progress_callback is not None:
            progress_callback(summary.copy())

        for site_id in eligible_site_ids:
            if should_cancel is not None and should_cancel():
                break
            try:
                activation = self._activate_for_site(actor=actor, site_id=site_id, purpose="version check")
            except CrocoblockLicenseError as exc:
                summary["failed"] += 1
                summary["completed"] += 1
                dashboard_diagnostic = (
                    self._diagnose_crocoblock_dashboard(site_id)
                    if exc.code == CROCOBLOCK_DASHBOARD_UNAVAILABLE_CODE
                    else {}
                )
                if site_result_callback is not None:
                    site_result_callback(
                        {
                            "site_id": site_id,
                            "status": "activation-failed",
                            "detail": self._activation_failure_detail(exc, dashboard_diagnostic),
                            "license_was_already_active": None,
                            "update_package_ready": None,
                            "provider_versions": [],
                            "dashboard_diagnostic": dashboard_diagnostic,
                        }
                    )
                if progress_callback is not None:
                    progress_callback(summary.copy())
                continue

            summary["activated"] += 1
            summary["versions"].extend(activation["provider_versions"])
            try:
                SiteUpdateService(db=self.db, cipher=self.cipher).refresh_site_updates(site_id)
            except SiteMcpProxyError as exc:
                summary["failed"] += 1
                summary["completed"] += 1
                if site_result_callback is not None:
                    site_result_callback(
                        {
                            "site_id": site_id,
                            "status": "activation-verified-update-refresh-failed",
                            "detail": f"{self._license_check_detail(activation)} Update inventory refresh failed: {exc.message}",
                            "license_was_already_active": activation["license_was_already_active"],
                            "update_package_ready": activation["update_package_ready"],
                            "provider_versions": activation["provider_versions"],
                        }
                    )
                if progress_callback is not None:
                    progress_callback(summary.copy())
                continue
            summary["refreshed"] += 1
            summary["completed"] += 1
            if site_result_callback is not None:
                site_result_callback(
                    {
                        "site_id": site_id,
                        "status": "activation-verified",
                        "detail": self._license_check_detail(activation),
                        "license_was_already_active": activation["license_was_already_active"],
                        "update_package_ready": activation["update_package_ready"],
                        "provider_versions": activation["provider_versions"],
                    }
                )
            if progress_callback is not None:
                progress_callback(summary.copy())

        return summary

    def _activate_for_site(self, *, actor: str, site_id: int, purpose: str) -> dict[str, object]:
        config = self.get_config()
        if config is None or not config.enabled:
            raise CrocoblockLicenseError("Save a Crocoblock license in Account before using Jet Dashboard update metadata.")

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
                actor=actor,
                source="hub-worker",
                action="activate-crocoblock-license",
                result="failed",
                detail=f"Crocoblock license activation could not run for {site.domain}: {exc.code}.",
            )
            self.db.commit()
            raise CrocoblockLicenseError(exc.message, code=exc.code) from exc

        result = payload.get("result")
        if not isinstance(result, dict) or result.get("activated") is not True or result.get("site_activated") is not True:
            config.last_error = "activation_not_verified"
            write_audit_log(
                self.db,
                site=site,
                actor=actor,
                source="hub-worker",
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
            actor=actor,
            source="hub-worker",
            action="activate-crocoblock-license",
            result="success",
            detail=(
                f"Activated the centrally stored Crocoblock license for {site.domain} "
                f"for a Jet {purpose}. The license key was not logged."
            ),
        )
        self.db.commit()
        return {
            "site": site,
            "license_was_already_active": result.get("license_was_already_active")
            if isinstance(result.get("license_was_already_active"), bool)
            else None,
            "update_package_ready": result.get("update_package_ready") is True,
            "provider_versions": self._provider_versions(result),
        }

    @staticmethod
    def _license_check_detail(activation: dict[str, object]) -> str:
        was_already_active = activation.get("license_was_already_active")
        if was_already_active is True:
            detail = "The Crocoblock license was already active before this check; update availability was refreshed."
        elif was_already_active is False:
            detail = "The stored Crocoblock license was not active and was activated for this website."
        else:
            detail = "Crocoblock license activation was verified; this Bridge version did not report the prior license state."
        if activation.get("update_package_ready") is True:
            return f"{detail} An authorized update package is now available."
        return f"{detail} An authorized update package is still unavailable."

    def _diagnose_crocoblock_dashboard(self, site_id: int) -> dict[str, object]:
        """Collect a fixed, read-only compatibility report after manager setup failed."""
        try:
            payload = self.proxy.execute_readonly_ability(
                site_id,
                CROCOBLOCK_DASHBOARD_DIAGNOSTIC_ABILITY,
                None,
                timeout_seconds=30,
            )
        except SiteMcpProxyError as exc:
            if exc.code == "KOSMOS_BRIDGE_ABILITY_NOT_FOUND":
                return {
                    "status": "bridge-upgrade-required",
                    "message": (
                        "This site has an older Kosmos Bridge without the read-only Jet Dashboard diagnostic. "
                        "Update Kosmos Bridge to 0.3.57 and run the automatic refresh again."
                    ),
                    "error_code": exc.code,
                }
            return {
                "status": "unavailable",
                "message": f"The Jet Dashboard diagnostic could not be read: {exc.message}",
                "error_code": exc.code,
            }

        result = payload.get("result")
        if not isinstance(result, dict):
            return {
                "status": "invalid-response",
                "message": "The Jet Dashboard diagnostic returned no compatible report.",
            }

        boolean_fields = (
            "dashboard_class_available",
            "utils_class_available",
            "dashboard_instance_available",
            "init_managers_available",
            "init_managers_called",
            "init_managers_failed",
            "license_manager_available",
            "plugin_manager_available",
        )
        numeric_fields = (
            "bootstrap_hooks_found",
            "bootstrap_hooks_invoked",
            "bootstrap_hook_errors",
        )
        allowed_methods = {
            "license_manager_methods": {"license_action_query", "update_license_list"},
            "plugin_manager_methods": {"get_remote_jet_plugin_list"},
        }
        diagnostic: dict[str, object] = {
            "status": "captured",
            "message": str(result.get("message", "Jet Dashboard compatibility report was captured."))[:500],
        }
        for field in boolean_fields:
            diagnostic[field] = result.get(field) is True
        for field in numeric_fields:
            value = result.get(field)
            diagnostic[field] = value if isinstance(value, int) and value >= 0 else 0
        for field, allowed in allowed_methods.items():
            values = result.get(field)
            diagnostic[field] = [value for value in values if isinstance(value, str) and value in allowed] if isinstance(values, list) else []
        return diagnostic

    @staticmethod
    def _activation_failure_detail(exc: CrocoblockLicenseError, dashboard_diagnostic: dict[str, object]) -> str:
        detail = str(exc)
        message = dashboard_diagnostic.get("message")
        if isinstance(message, str) and message:
            return f"{detail} Jet Dashboard diagnosis: {message}"
        return detail

    def _require_admin(self, actor: HubUser) -> None:
        if actor.role != "admin":
            raise CrocoblockLicenseError("Only Hub administrators can manage provider licenses.")

    @staticmethod
    def _normalize_license_key(value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8 or len(normalized) > 512 or any(character.isspace() for character in normalized):
            raise CrocoblockLicenseError("Enter a valid Crocoblock license key without spaces.")
        return normalized

    @staticmethod
    def is_jet_plugin_file(plugin_file: str) -> bool:
        return plugin_file.strip().startswith("jet-")

    @classmethod
    def _provider_versions(cls, result: dict[str, object]) -> list[dict[str, str]]:
        raw_versions = result.get("plugins", [])
        if not isinstance(raw_versions, list):
            return []

        versions: list[dict[str, str]] = []
        for entry in raw_versions:
            if not isinstance(entry, dict):
                continue
            plugin_file = str(entry.get("plugin_file", "")).strip()
            version = str(entry.get("version", "")).strip()
            if not cls.is_jet_plugin_file(plugin_file) or not version:
                continue
            versions.append({"plugin_file": plugin_file, "version": version})
        return versions
