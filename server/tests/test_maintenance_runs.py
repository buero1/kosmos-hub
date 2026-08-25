from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.maintenance_run import MaintenanceRun, MaintenanceRunStatus
from app.models.site import Site, SiteStatus
from app.services.maintenance_runs import MaintenanceRunService
from app.services.site_mcp_proxy import SiteMcpProxyService


def test_updraftplus_backup_run_is_started_and_verified(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    backup_nonce = "a1b2c3d4e5f6"

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert site_id == 1
        assert ability_name == "kosmos-bridge/start-updraftplus-backup"
        assert ability_input == {}
        return {
            "result": {
                "accepted": True,
                "backup_nonce": backup_nonce,
                "scheduled_at": "2026-08-25T12:00:00+00:00",
            }
        }

    def execute_readonly_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert site_id == 1
        assert ability_name == "kosmos-bridge/get-updraftplus-backup-status"
        assert ability_input == {"backup_nonce": backup_nonce}
        return {
            "result": {
                "reported_at": "2026-08-25T12:02:00+00:00",
                "installed": True,
                "active": True,
                "available": True,
                "complete": True,
                "backup_nonce": backup_nonce,
                "latest_backup_at": "2026-08-25T12:01:30+00:00",
                "backup_count": 4,
                "components": ["database", "plugins", "themes", "uploads", "others"],
                "message": "Complete UpdraftPlus backup found.",
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    monkeypatch.setattr(SiteMcpProxyService, "execute_readonly_ability", execute_readonly_ability)

    with Session(engine) as db:
        site = Site(
            uuid="12ab34cd-56ef-78ab-90cd-12ef34ab56cd",
            domain="test.example",
            home_url="https://test.example/",
            site_url="https://test.example/",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=SecretCipher("a" * 32))
        started = service.start_updraftplus_backup(site_id=site.id, actor="operator")

        assert started.result == "started"
        assert started.run.status == MaintenanceRunStatus.running.value
        assert started.run.bridge_backup_nonce == backup_nonce

        summary = service.poll_active_updraftplus_backups()
        verified = db.get(MaintenanceRun, started.run.id)

        assert summary == {"checked": 1, "succeeded": 1, "failed": 0, "waiting": 0}
        assert verified is not None
        assert verified.status == MaintenanceRunStatus.succeeded.value
        assert verified.result_json["backup_nonce"] == backup_nonce
        assert verified.result_json["components"] == ["database", "plugins", "themes", "uploads", "others"]


def test_second_running_backup_run_is_blocked(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {
            "result": {
                "accepted": True,
                "backup_nonce": "a1b2c3d4e5f6",
                "scheduled_at": "2026-08-25T12:00:00+00:00",
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)

    with Session(engine) as db:
        site = Site(
            uuid="87ab34cd-56ef-78ab-90cd-12ef34ab56cd",
            domain="test.example",
            home_url="https://test.example/",
            site_url="https://test.example/",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=SecretCipher("a" * 32))
        first = service.start_updraftplus_backup(site_id=site.id, actor="operator")
        second = service.start_updraftplus_backup(site_id=site.id, actor="operator")

        assert first.result == "started"
        assert second.result == "blocked"
        assert second.run.id == first.run.id
