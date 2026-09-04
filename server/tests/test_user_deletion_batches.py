from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.site import Site, SiteStatus
from app.models.site_capability import SiteCapability
from app.services.site_mcp_proxy import SiteMcpProxyError
from app.services.site_users import SiteUserService
from app.services.user_deletion_batches import UserDeletionBatchService


def _site() -> Site:
    return Site(
        uuid="cccb34cd-56ef-78ab-90cd-12ef34ab56cd",
        domain="delete-users.example",
        home_url="https://delete-users.example/",
        site_url="https://delete-users.example/",
        status=SiteStatus.verified.value,
    )


def _users() -> list[dict]:
    return [
        {
            "id": 7,
            "username": "departing-editor",
            "display_name": "Departing Editor",
            "email": "departing@example.test",
            "roles": ["editor"],
            "registered_at": "2026-08-27T10:00:00+00:00",
        },
        {
            "id": 12,
            "username": "admin-alpha",
            "display_name": "Admin Alpha",
            "email": "alpha@example.test",
            "roles": ["administrator"],
            "registered_at": "2026-08-27T10:00:00+00:00",
        },
        {
            "id": 14,
            "username": "admin-zulu",
            "display_name": "Admin Zulu",
            "email": "zulu@example.test",
            "roles": ["administrator"],
            "registered_at": "2026-08-27T10:00:00+00:00",
        },
    ]


def _setup(db: Session, *, users: list[dict] | None = None) -> tuple[Site, SecretCipher]:
    cipher = SecretCipher("a" * 32)
    site = _site()
    db.add(site)
    db.flush()
    db.add(
        SiteCapability(
            site_id=site.id,
            capability="delete-wp-user",
            provider="kosmos-wordpress",
            ability_name=SiteUserService.DELETE_ABILITY,
            ability_schema={},
            read_only=False,
            destructive=True,
            last_discovered_at=datetime.now(UTC),
        )
    )
    db.commit()
    SiteUserService(db=db, cipher=cipher)._store_inventory(
        site.id,
        available=True,
        users=users or _users(),
        message=None,
        actor="admin",
    )
    return site, cipher


def test_prepare_defaults_to_another_administrator_and_excludes_selected_users():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        site, cipher = _setup(db)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(
            selected_keys=[f"{site.id}:7", f"{site.id}:12"],
            actor="kosmosadmin",
        )
        db.commit()

        assert [item.status for item in batch.items] == [service.ITEM_READY, service.ITEM_READY]
        assert [item.replacement_user_id for item in batch.items] == [14, 14]
        rows = service.preparation_rows(batch)
        assert [[candidate.user["id"] for candidate in row["candidates"]] for row in rows] == [[14], [14]]


def test_prepare_blocks_deletion_when_no_other_administrator_is_available():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    only_admin = _users()[1]

    with Session(engine) as db:
        site, cipher = _setup(db, users=[only_admin])
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(selected_keys=[f"{site.id}:12"], actor="kosmosadmin")

        assert batch.items[0].status == service.ITEM_BLOCKED
        assert "No other stored administrator" in batch.items[0].message
        assert service.batch_can_start(batch, service.preparation_rows(batch)) is False


def test_prepare_allows_more_than_ten_deletions_only_with_typed_confirmation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    many_users = [
        {
            "id": user_id,
            "username": f"departing-{user_id}",
            "display_name": f"Departing {user_id}",
            "email": f"departing-{user_id}@example.test",
            "roles": ["editor"],
            "registered_at": "2026-08-27T10:00:00+00:00",
        }
        for user_id in range(1, UserDeletionBatchService.TEXT_CONFIRMATION_THRESHOLD + 2)
    ]
    many_users.append(
        {
            "id": 99,
            "username": "remaining-admin",
            "display_name": "Remaining Admin",
            "email": "remaining-admin@example.test",
            "roles": ["administrator"],
            "registered_at": "2026-08-27T10:00:00+00:00",
        }
    )

    with Session(engine) as db:
        site, cipher = _setup(db, users=many_users)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        selected_keys = [f"{site.id}:{user_id}" for user_id in range(1, UserDeletionBatchService.TEXT_CONFIRMATION_THRESHOLD + 2)]

        unconfirmed_batch = service.prepare_batch(selected_keys=selected_keys[:-1], actor="kosmosadmin")
        assert len(unconfirmed_batch.items) == UserDeletionBatchService.TEXT_CONFIRMATION_THRESHOLD

        with pytest.raises(ValueError, match='Type "delete"'):
            service.prepare_batch(selected_keys=selected_keys, actor="kosmosadmin")

        batch = service.prepare_batch(
            selected_keys=selected_keys,
            actor="kosmosadmin",
            deletion_confirmation="delete",
        )

        assert len(batch.items) == UserDeletionBatchService.TEXT_CONFIRMATION_THRESHOLD + 1
        assert {item.replacement_user_id for item in batch.items} == {99}


def test_start_rejects_a_non_administrator_reassignment_target():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        site, cipher = _setup(db)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(selected_keys=[f"{site.id}:7"], actor="kosmosadmin")
        db.commit()

        with pytest.raises(ValueError, match="valid other administrator"):
            service.start_batch(
                batch_id=batch.id,
                item_ids=[batch.items[0].id],
                replacement_user_ids=[7],
            )


def test_claimed_deletion_refreshes_then_deletes_with_the_chosen_administrator(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls: list[dict] = []

    def refresh_site_users(self, site_id, *, actor="kosmos-hub"):
        inventory = self.get_latest_inventory(site_id)
        assert inventory is not None
        return inventory

    def delete_user(self, **kwargs):
        calls.append(kwargs)
        return {"deleted": True}

    monkeypatch.setattr(SiteUserService, "refresh_site_users", refresh_site_users)
    monkeypatch.setattr(SiteUserService, "delete_user", delete_user)

    with Session(engine) as db:
        site, cipher = _setup(db)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(selected_keys=[f"{site.id}:7"], actor="kosmosadmin")
        db.commit()
        service.start_batch(
            batch_id=batch.id,
            item_ids=[batch.items[0].id],
            replacement_user_ids=[12],
        )
        db.commit()

        item_id = service.claim_next_item_id()
        assert item_id is not None
        assert service.process_claimed_item(item_id) == service.ITEM_SUCCEEDED

        completed = service.get_batch(batch.id)
        assert completed is not None
        assert completed.status == service.BATCH_COMPLETED
        assert calls == [
            {
                "site_id": site.id,
                "user_id": 7,
                "reassign_to_user_id": 12,
                "confirmed_username": "departing-editor",
                "actor": "kosmosadmin",
                "refresh_inventory": False,
            }
        ]
        assert service.status_payload(completed)["completed"] == 1


def test_cancelled_batch_marks_queued_users_without_calling_wordpress():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        site, cipher = _setup(db)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(selected_keys=[f"{site.id}:7"], actor="kosmosadmin")
        db.commit()
        service.start_batch(
            batch_id=batch.id,
            item_ids=[batch.items[0].id],
            replacement_user_ids=[12],
        )
        db.commit()

        cancelled = service.cancel_batch(batch_id=batch.id)
        db.commit()
        assert cancelled.status == service.BATCH_CANCELLED
        assert cancelled.items[0].status == service.ITEM_CANCELLED


def test_prepared_batch_can_be_cancelled_without_starting_wordpress_work():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        site, cipher = _setup(db)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(selected_keys=[f"{site.id}:7"], actor="kosmosadmin")
        db.commit()

        cancelled = service.cancel_batch(batch_id=batch.id)
        db.commit()
        assert cancelled.status == service.BATCH_CANCELLED
        assert cancelled.items[0].status == service.ITEM_CANCELLED


def test_successful_deletion_stays_successful_when_the_final_inventory_refresh_fails(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    refresh_count = 0

    def refresh_site_users(self, site_id, *, actor="kosmos-hub"):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 2:
            raise SiteMcpProxyError("BRIDGE_UNAVAILABLE", "Bridge did not answer after deletion.", status_code=502)
        inventory = self.get_latest_inventory(site_id)
        assert inventory is not None
        return inventory

    monkeypatch.setattr(SiteUserService, "refresh_site_users", refresh_site_users)
    monkeypatch.setattr(SiteUserService, "delete_user", lambda self, **kwargs: {"deleted": True})

    with Session(engine) as db:
        site, cipher = _setup(db)
        service = UserDeletionBatchService(db=db, cipher=cipher)
        batch = service.prepare_batch(selected_keys=[f"{site.id}:7"], actor="kosmosadmin")
        db.commit()
        service.start_batch(
            batch_id=batch.id,
            item_ids=[batch.items[0].id],
            replacement_user_ids=[12],
        )
        db.commit()

        item_id = service.claim_next_item_id()
        assert item_id is not None
        assert service.process_claimed_item(item_id) == service.ITEM_SUCCEEDED
        assert "could not refresh" in service.get_batch(batch.id).items[0].message


def test_users_template_compiles_with_the_inline_deletion_batch_panel():
    from app.api.routes.web import templates

    assert templates.get_template("users.html") is not None


def test_user_deletion_batch_url_preserves_the_users_scope_and_filters():
    from app.api.routes.web import _user_deletion_batch_url

    url = _user_deletion_batch_url(
        42,
        return_to="/users?site_scope=selected&site_id=3&site_id=43&q=admin&role=administrator&customer_status=Aktuell",
    )

    assert url.startswith("/users?site_scope=selected&site_id=3&site_id=43&q=admin&role=administrator")
    assert "deletion_batch_id=42" in url
