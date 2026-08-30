import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.models.site import Site, SiteStatus
from app.repositories.site_repository import SiteRepository
from app.services.audit import write_audit_log
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService


@dataclass(frozen=True)
class SiteAdminLaunch:
    launch_url: str
    access_user_created: bool
    white_label_access_granted: bool


class SiteAdminLaunchService:
    """Creates a signed, one-time WordPress admin launch through the Bridge."""

    PREPARE_ABILITY = "kosmos-bridge/prepare-admin-launch"
    MINIMUM_BRIDGE_VERSION = (0, 3, 62)

    def __init__(self, *, db: Session, cipher: SecretCipher):
        self.db = db
        self.repository = SiteRepository(db)
        self.proxy = SiteMcpProxyService(db=db, cipher=cipher)

    def open_admin(self, *, site_id: int, actor: str) -> SiteAdminLaunch:
        site = self.repository.get_site(site_id)
        if site is None:
            raise ValueError("The selected website no longer exists.")
        if site.status != SiteStatus.verified.value:
            raise ValueError("WordPress admin access is available only for verified websites.")
        if not self.bridge_supports_launch(site.bridge_version):
            raise ValueError("Update this website to Kosmos Bridge 0.3.62 or newer before opening its WordPress backend.")

        payload = self.proxy.execute_ability(site_id, self.PREPARE_ABILITY, {}, timeout_seconds=20)
        result = payload.get("result") if isinstance(payload, dict) else None
        result = result if isinstance(result, dict) else {}
        launch_url = self._trusted_launch_url(site, result.get("launch_url"))
        access_user_created = result.get("access_user_created") is True
        white_label_access_granted = result.get("white_label_access_granted") is True

        write_audit_log(
            self.db,
            site=site,
            actor=actor,
            source="hub-web",
            action="open-wordpress-admin",
            result="ok",
            detail=(
                "Added the dedicated Kosmos Hub user to the active White Label CMS administrator list and opened a one-time WordPress admin launch."
                if white_label_access_granted
                else "Opened a one-time WordPress admin launch through Kosmos Bridge."
            )
            if not access_user_created
            else (
                "Created the dedicated local Kosmos Hub access user, added it to the active White Label CMS administrator list, and opened a one-time WordPress admin launch."
                if white_label_access_granted
                else "Created the dedicated local Kosmos Hub access user and opened a one-time WordPress admin launch."
            ),
        )
        self.db.commit()
        return SiteAdminLaunch(
            launch_url=launch_url,
            access_user_created=access_user_created,
            white_label_access_granted=white_label_access_granted,
        )

    @classmethod
    def bridge_supports_launch(cls, version: object) -> bool:
        if not isinstance(version, str):
            return False
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version.strip())
        if match is None:
            return False
        return tuple(int(part) for part in match.groups()) >= cls.MINIMUM_BRIDGE_VERSION

    @staticmethod
    def _trusted_launch_url(site: Site, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SiteMcpProxyError(
                "ADMIN_LAUNCH_URL_MISSING",
                "The Bridge did not return a WordPress admin launch URL.",
                status_code=502,
            )

        launch_url = value.strip()
        target = urlsplit(launch_url)
        expected = urlsplit(site.home_url)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.hostname.casefold() != (expected.hostname or "").casefold()
            or target.port != expected.port
            or not parse_qs(target.query).get("kosmos_admin_launch")
        ):
            raise SiteMcpProxyError(
                "ADMIN_LAUNCH_URL_UNTRUSTED",
                "The Bridge returned an invalid WordPress admin launch URL.",
                status_code=502,
            )
        return launch_url
