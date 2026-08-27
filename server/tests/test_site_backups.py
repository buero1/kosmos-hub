from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.site import Site, SiteStatus
from app.services.site_backups import SiteBackupService
from app.services.site_mcp_proxy import SiteMcpProxyService


def test_backup_refresh_stores_every_updraftplus_backup_set(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert ability_name == "kosmos-bridge/list-updraftplus-backups"
        assert ability_input == {}
        return {
            "result": {
                "installed": True,
                "active": True,
                "message": "UpdraftPlus backup metadata was read successfully.",
                "backups": [
                    {
                        "backup_at": "2026-08-27T10:00:00+00:00",
                        "complete": False,
                        "retention_protected": False,
                        "components": ["database"],
                    },
                    {
                        "backup_at": "2026-08-27T12:00:00+00:00",
                        "complete": True,
                        "retention_protected": True,
                        "components": ["database", "plugins", "themes", "uploads", "others"],
                    },
                ],
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)

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

        snapshot = SiteBackupService(db=db, cipher=SecretCipher("a" * 32)).refresh_site_backup_status(site.id)["snapshot"]

        assert snapshot.backup_count == 2
        assert snapshot.backup_at.isoformat() == "2026-08-27T12:00:00"
        assert snapshot.summary_json["backup_list_available"] is True
        assert [backup["complete"] for backup in snapshot.summary_json["backups"]] == [True, False]
