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
        assert timeout_seconds == MaintenanceRunService.START_BACKUP_TIMEOUT_SECONDS
        return {
            "result": {
                "accepted": True,
                "backup_nonce": backup_nonce,
                "retention_protection_requested": True,
                "request_status": "queued",
                "background_dispatch_requested": True,
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
                "retention_protected": True,
                "request_status": "completed",
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
        assert service.START_BACKUP_TIMEOUT_SECONDS == 20
        assert service.BACKUP_TIMEOUT.total_seconds() == 180
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
        assert verified.result_json["retention_protected"] is True
        assert verified.result_json["bridge_status"] == "completed"


def test_second_running_backup_run_is_blocked(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {
            "result": {
                "accepted": True,
                "backup_nonce": "a1b2c3d4e5f6",
                "retention_protection_requested": True,
                "request_status": "queued",
                "background_dispatch_requested": True,
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


def test_unprotected_completed_backup_run_fails(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    backup_nonce = "a1b2c3d4e5f6"

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {
            "result": {
                "accepted": True,
                "backup_nonce": backup_nonce,
                "retention_protection_requested": True,
                "request_status": "queued",
                "background_dispatch_requested": True,
                "scheduled_at": "2026-08-25T12:00:00+00:00",
            }
        }

    def execute_readonly_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {
            "result": {
                "installed": True,
                "active": True,
                "available": True,
                "complete": True,
                "retention_protected": False,
                "request_status": "completed",
                "backup_nonce": backup_nonce,
                "latest_backup_at": "2026-08-25T12:01:30+00:00",
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    monkeypatch.setattr(SiteMcpProxyService, "execute_readonly_ability", execute_readonly_ability)

    with Session(engine) as db:
        site = Site(
            uuid="34ab34cd-56ef-78ab-90cd-12ef34ab56cd",
            domain="test.example",
            home_url="https://test.example/",
            site_url="https://test.example/",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=SecretCipher("a" * 32))
        started = service.start_updraftplus_backup(site_id=site.id, actor="operator")

        summary = service.poll_active_updraftplus_backups()

        assert started.result == "started"
        assert summary == {"checked": 1, "succeeded": 0, "failed": 1, "waiting": 0}
        assert started.run.status == MaintenanceRunStatus.failed.value
        assert "not protected" in (started.run.error_message or "")


def test_queued_background_backup_reports_bridge_progress(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    backup_nonce = "a1b2c3d4e5f6"

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {
            "result": {
                "accepted": True,
                "backup_nonce": backup_nonce,
                "retention_protection_requested": True,
                "request_status": "queued",
                "background_dispatch_requested": True,
                "scheduled_at": "2026-08-25T12:00:00+00:00",
            }
        }

    def execute_readonly_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        return {
            "result": {
                "installed": True,
                "active": True,
                "available": False,
                "complete": False,
                "request_status": "starting",
                "request_message": "The WordPress background worker is starting the protected backup with UpdraftPlus.",
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    monkeypatch.setattr(SiteMcpProxyService, "execute_readonly_ability", execute_readonly_ability)

    with Session(engine) as db:
        site = Site(
            uuid="56ab34cd-56ef-78ab-90cd-12ef34ab56cd",
            domain="test.example",
            home_url="https://test.example/",
            site_url="https://test.example/",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=SecretCipher("a" * 32))
        started = service.start_updraftplus_backup(site_id=site.id, actor="operator")

        summary = service.poll_active_updraftplus_backups()

        assert summary == {"checked": 1, "succeeded": 0, "failed": 0, "waiting": 1}
        assert started.run.status == MaintenanceRunStatus.running.value
        assert started.run.result_json["bridge_status"] == "starting"
        assert "background worker" in started.run.result_json["bridge_status_message"]
