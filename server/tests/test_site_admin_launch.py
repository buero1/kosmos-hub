from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.site import Site, SiteStatus
from app.services.site_admin_launch import SiteAdminLaunchService
from app.services.site_mcp_proxy import SiteMcpProxyService


def _site() -> Site:
    return Site(
        uuid="abcb34cd-56ef-78ab-90cd-12ef34ab56cd",
        domain="launch.example",
        home_url="https://launch.example/",
        site_url="https://launch.example/",
        status=SiteStatus.verified.value,
        bridge_version="0.3.62",
    )


def test_admin_launch_uses_the_bridge_ticket_and_records_audit_entry(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert ability_name == "kosmos-bridge/prepare-admin-launch"
        assert ability_input == {}
        return {
            "result": {
                "launch_url": "https://launch.example/?kosmos_admin_launch=abc.def",
                "access_user_created": True,
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.commit()

        launch = SiteAdminLaunchService(db=db, cipher=SecretCipher("a" * 32)).open_admin(site_id=site.id, actor="operator")

        assert launch.launch_url == "https://launch.example/?kosmos_admin_launch=abc.def"
        assert launch.access_user_created is True
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "open-wordpress-admin"))
        assert audit is not None
        assert audit.actor == "operator"
        assert "dedicated local" in audit.detail


def test_admin_launch_rejects_a_bridge_url_for_another_domain(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {"result": {"launch_url": "https://other.example/?kosmos_admin_launch=abc.def"}}

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.commit()

        try:
            SiteAdminLaunchService(db=db, cipher=SecretCipher("a" * 32)).open_admin(site_id=site.id, actor="operator")
        except Exception as exc:
            assert getattr(exc, "code", "") == "ADMIN_LAUNCH_URL_UNTRUSTED"
        else:
            raise AssertionError("An admin launch URL for another domain must be rejected.")


def test_admin_launch_requires_bridge_0362_or_newer():
    assert SiteAdminLaunchService.bridge_supports_launch("0.3.61") is False
    assert SiteAdminLaunchService.bridge_supports_launch("0.3.62") is True
    assert SiteAdminLaunchService.bridge_supports_launch("0.4.0") is True
