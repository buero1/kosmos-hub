from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.maintenance_run import MaintenanceRun, MaintenanceRunStatus, MaintenanceRunStep, MaintenanceRunStepStatus
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
        if ability_name == "kosmos-bridge/get-updraftplus-backup-status":
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
                    "backup_count": 1,
                    "components": ["database", "plugins", "themes", "uploads", "others"],
                    "message": "Complete UpdraftPlus backup found.",
                }
            }
        assert ability_name == "kosmos-bridge/list-updraftplus-backups"
        assert ability_input == {}
        return {
            "result": {
                "installed": True,
                "active": True,
                "backups": [
                    {
                        "backup_nonce": backup_nonce,
                        "backup_timestamp": 1756123290,
                        "backup_at": "2026-08-25T12:01:30+00:00",
                        "complete": True,
                        "retention_protected": True,
                    }
                ],
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
        assert verified.result_json["cleanup"]["status"] == "skipped"


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


def test_verified_backup_prunes_only_oldest_manually_protected_complete_backup(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    backup_nonce = "a1b2c3d4e5f6"
    partial_backup_nonce = "d1b2c3d4e5f6"
    old_complete_nonce = "b1b2c3d4e5f6"
    deleted_inputs = []

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert site_id == 1
        if ability_name == "kosmos-bridge/start-updraftplus-backup":
            assert ability_input == {}
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

        assert ability_name == "kosmos-bridge/delete-updraftplus-backup"
        assert timeout_seconds == MaintenanceRunService.DELETE_BACKUP_TIMEOUT_SECONDS
        deleted_inputs.append(ability_input)
        return {
            "result": {
                "status": "completed",
                "completed": True,
                "backup_sets_removed": 1,
                "local_files_deleted": 5,
                "remote_files_deleted": 5,
                "message": "UpdraftPlus deleted the requested backup locally and from the configured remote storage.",
            }
        }

    def execute_readonly_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert site_id == 1
        if ability_name == "kosmos-bridge/get-updraftplus-backup-status":
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
                    "backup_count": 6,
                    "components": ["database", "plugins", "themes", "uploads", "others"],
                }
            }

        if ability_name == "kosmos-bridge/verify-updraftplus-backup-deletion":
            assert ability_input == {
                "backup_nonce": old_complete_nonce,
                "backup_timestamp": 1700000000,
            }
            return {
                "result": {
                    "backup_nonce": old_complete_nonce,
                    "backup_timestamp": 1700000000,
                    "verified": True,
                    "remaining_components": [],
                    "message": "UpdraftPlus remote rescan confirmed that the requested backup set is no longer present.",
                }
            }

        assert ability_name == "kosmos-bridge/list-updraftplus-backups"
        assert ability_input == {}
        return {
            "result": {
                "installed": True,
                "active": True,
                "backups": [
                    {
                        "backup_nonce": partial_backup_nonce,
                        "backup_timestamp": 1600000000,
                        "backup_at": "2020-09-13T12:26:40+00:00",
                        "complete": False,
                        "retention_protected": True,
                    },
                    {
                        "backup_nonce": "f1b2c3d4e5f6",
                        "backup_timestamp": 1650000000,
                        "backup_at": "2022-04-15T05:20:00+00:00",
                        "complete": True,
                        "retention_protected": False,
                    },
                    {
                        "backup_nonce": old_complete_nonce,
                        "backup_timestamp": 1700000000,
                        "backup_at": "2023-11-14T22:13:20+00:00",
                        "complete": True,
                        "retention_protected": True,
                    },
                    {
                        "backup_nonce": "c1b2c3d4e5f6",
                        "backup_timestamp": 1720000000,
                        "backup_at": "2024-07-03T09:46:40+00:00",
                        "complete": True,
                        "retention_protected": True,
                    },
                    {
                        "backup_nonce": backup_nonce,
                        "backup_timestamp": 1756123290,
                        "backup_at": "2025-08-25T12:01:30+00:00",
                        "complete": True,
                        "retention_protected": True,
                    },
                    {
                        "backup_nonce": "e1b2c3d4e5f6",
                        "backup_timestamp": 1756123300,
                        "backup_at": "2025-08-25T12:01:40+00:00",
                        "complete": False,
                        "retention_protected": True,
                    },
                ],
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    monkeypatch.setattr(SiteMcpProxyService, "execute_readonly_ability", execute_readonly_ability)

    with Session(engine) as db:
        site = Site(
            uuid="98ab34cd-56ef-78ab-90cd-12ef34ab56cd",
            domain="test.example",
            home_url="https://test.example/",
            site_url="https://test.example/",
            status=SiteStatus.verified.value,
        )
        db.add(site)
        db.commit()

        service = MaintenanceRunService(db=db, cipher=SecretCipher("a" * 32))
        started = service.start_updraftplus_backup(site_id=site.id, actor="operator")
        first_poll = service.poll_active_updraftplus_backups()
        second_poll = service.poll_active_updraftplus_backups()
        verified = db.get(MaintenanceRun, started.run.id)

        assert first_poll == {"checked": 1, "succeeded": 0, "failed": 0, "waiting": 1}
        assert second_poll == {"checked": 1, "succeeded": 1, "failed": 0, "waiting": 0}
        assert deleted_inputs == [
            {
                "backup_nonce": old_complete_nonce,
                "backup_timestamp": 1700000000,
                "delete_remote": True,
                "allow_protected_delete": True,
            },
        ]
        assert verified is not None
        assert verified.status == MaintenanceRunStatus.succeeded.value
        assert verified.result_json["cleanup"] == {
            "status": "completed",
            "backup_nonce": old_complete_nonce,
            "backup_timestamp": 1700000000,
            "backup_at": "2023-11-14T22:13:20+00:00",
            "backup_sets_removed": 1,
            "local_files_deleted": 5,
            "remote_files_deleted": 5,
            "message": "UpdraftPlus remote rescan confirmed that the requested backup set is no longer present.",
            "remote_deletion_verified": True,
        }


def test_remote_deletion_verification_fails_when_components_remain(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    backup_nonce = "b1b2c3d4e5f6"

    def execute_readonly_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert site_id == 1
        assert ability_name == "kosmos-bridge/verify-updraftplus-backup-deletion"
        assert ability_input == {"backup_nonce": backup_nonce, "backup_timestamp": 1700000000}
        return {
            "result": {
                "backup_nonce": backup_nonce,
                "backup_timestamp": 1700000000,
                "verified": False,
                "remaining_components": ["database", "themes", "uploads", "others"],
                "message": "UpdraftPlus remote rescan still found files from the requested backup set.",
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_readonly_ability", execute_readonly_ability)

    with Session(engine) as db:
        site = Site(
            uuid="29ab34cd-56ef-78ab-90cd-12ef34ab56cd",
            domain="test.example",
            home_url="https://test.example/",
            site_url="https://test.example/",
            status=SiteStatus.verified.value,
        )
        run = MaintenanceRun(
            site=site,
            kind=MaintenanceRunService.UPDRAFT_BACKUP_KIND,
            status=MaintenanceRunStatus.running.value,
            requested_by="operator",
            bridge_backup_nonce="a1b2c3d4e5f6",
            started_at=datetime.now(UTC),
            result_json={},
        )
        cleanup = {
            "status": "verifying",
            "backup_nonce": backup_nonce,
            "backup_timestamp": 1700000000,
            "backup_at": "2023-11-14T22:13:20+00:00",
            "message": "UpdraftPlus reported deletion.",
        }
        cleanup_step = MaintenanceRunStep(
            run=run,
            step_key="prune-oldest-backup",
            status=MaintenanceRunStepStatus.running.value,
            started_at=datetime.now(UTC),
            detail=cleanup["message"],
            result_json=cleanup,
        )
        run.result_json = {"cleanup": cleanup}
        db.add_all((site, run, cleanup_step))
        db.commit()

        service = MaintenanceRunService(db=db, cipher=SecretCipher("a" * 32))
        result = service._verify_updraftplus_backup_cleanup(run, cleanup_step, cleanup)

        assert result == "failed"
        assert run.status == MaintenanceRunStatus.failed.value
        assert cleanup_step.status == MaintenanceRunStepStatus.failed.value
        assert "database, themes, uploads, others" in (run.error_message or "")


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
