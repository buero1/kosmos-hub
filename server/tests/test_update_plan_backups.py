from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.update_plans import UpdatePlanService


def backup_snapshot(**overrides):
    values = {
        "provider": "updraftplus",
        "provider_installed": True,
        "provider_active": True,
        "backup_available": True,
        "backup_complete": True,
        "backup_at": datetime.now(UTC),
        "summary_json": {"retention_protected": True},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_preflight_requires_a_backup_check():
    assert UpdatePlanService._backup_preflight(None) == ("not checked", None, None)


def test_preflight_accepts_a_fresh_complete_backup():
    status, provider, backup_at = UpdatePlanService._backup_preflight(backup_snapshot())

    assert status == "available"
    assert provider == "updraftplus"
    assert backup_at is not None


def test_preflight_blocks_an_incomplete_backup():
    status, _, _ = UpdatePlanService._backup_preflight(backup_snapshot(backup_complete=False))

    assert status == "no complete backup"


def test_preflight_blocks_a_backup_older_than_seven_days():
    status, _, _ = UpdatePlanService._backup_preflight(
        backup_snapshot(backup_at=datetime.now(UTC) - timedelta(days=8))
    )

    assert status == "backup stale"


def test_preflight_blocks_a_backup_without_retention_protection():
    status, _, _ = UpdatePlanService._backup_preflight(
        backup_snapshot(summary_json={"retention_protected": False})
    )

    assert status == "backup not protected"
