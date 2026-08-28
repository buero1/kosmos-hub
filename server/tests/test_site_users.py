from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.site import Site, SiteStatus
from app.models.site_capability import SiteCapability
from app.services.site_mcp_proxy import SiteMcpProxyError, SiteMcpProxyService
from app.services.site_users import SiteUserService


def _site() -> Site:
    return Site(
        uuid="aaab34cd-56ef-78ab-90cd-12ef34ab56cd",
        domain="users.example",
        home_url="https://users.example/",
        site_url="https://users.example/",
        status=SiteStatus.verified.value,
    )


def test_user_refresh_encrypts_snapshot_and_never_stores_password(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        assert ability_name == "kosmos-bridge/list-wp-users"
        assert ability_input == {}
        return {
            "result": {
                "available": True,
                "message": "User data refreshed.",
                "users": [
                    {
                        "id": 7,
                        "username": "editor",
                        "display_name": "Website Editor",
                        "email": "editor@example.test",
                        "roles": ["editor"],
                        "registered_at": "2026-08-27T10:00:00+00:00",
                    }
                ],
            }
        }

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.commit()

        inventory = SiteUserService(db=db, cipher=cipher).refresh_site_users(site.id, actor="admin")
        assert inventory.snapshot.available is True
        assert inventory.snapshot.user_count == 1
        assert inventory.users[0]["username"] == "editor"
        assert "editor@example.test" not in inventory.snapshot.encrypted_users_json
        assert cipher.decrypt(inventory.snapshot.encrypted_users_json).startswith("[")


def test_older_bridge_records_user_inventory_as_unsupported_without_failing(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        raise SiteMcpProxyError("KOSMOS_BRIDGE_ABILITY_NOT_FOUND", "Ability is not available.", status_code=404)

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)

    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.commit()

        inventory = SiteUserService(db=db, cipher=SecretCipher("a" * 32)).refresh_site_users(site.id)
        assert inventory.snapshot.available is False
        assert inventory.snapshot.user_count == 0
        assert "0.3.53" in inventory.snapshot.message
        audit = db.scalar(select(AuditLog).where(AuditLog.site_id == site.id))
        assert audit is not None
        assert audit.result == "unsupported"


def test_user_workbench_entries_include_site_context_and_stored_capabilities():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.flush()
        db.add(
            SiteCapability(
                site_id=site.id,
                capability="update-wp-user-role",
                provider="kosmos-wordpress",
                ability_name="kosmos-bridge/update-wp-user-role",
                ability_schema={},
                read_only=False,
                destructive=False,
                last_discovered_at=datetime.now(UTC),
            )
        )
        db.commit()

        service = SiteUserService(db=db, cipher=cipher)
        service._store_inventory(
            site.id,
            available=True,
            users=[
                {
                    "id": 7,
                    "username": "editor",
                    "display_name": "Site Editor",
                    "email": "editor@example.test",
                    "roles": ["editor"],
                    "registered_at": "2026-08-27T10:00:00+00:00",
                }
            ],
            message=None,
            actor="admin",
        )

        entries = service.list_workbench_entries()
        assert len(entries) == 1
        assert entries[0].key == f"{site.id}:7"
        assert entries[0].site.domain == "users.example"
        assert entries[0].supports_role_change is True
        assert entries[0].supports_password_change is False


def test_role_change_uses_bridge_ability_and_verifies_returned_role(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls = []

    def execute_ability(self, site_id, ability_name, ability_input, *, timeout_seconds=20):
        calls.append((ability_name, ability_input))
        if ability_name == "kosmos-bridge/update-wp-user-role":
            assert ability_input == {"user_id": 7, "role": "editor"}
            return {
                "result": {
                    "role_updated": True,
                    "user": {
                        "id": 7,
                        "username": "editor",
                        "display_name": "Site Editor",
                        "email": "editor@example.test",
                        "roles": ["editor"],
                        "registered_at": "2026-08-27T10:00:00+00:00",
                    },
                }
            }
        assert ability_name == "kosmos-bridge/list-wp-users"
        return {"result": {"available": True, "users": [], "message": "Refreshed."}}

    monkeypatch.setattr(SiteMcpProxyService, "execute_ability", execute_ability)
    with Session(engine) as db:
        site = _site()
        db.add(site)
        db.commit()

        changed = SiteUserService(db=db, cipher=SecretCipher("a" * 32)).update_role(
            site_id=site.id,
            user_id=7,
            role="editor",
            actor="admin",
        )
        assert changed["roles"] == ["editor"]
        assert calls[0][0] == "kosmos-bridge/update-wp-user-role"
