from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.hub_access_control import HubAccessControlService, permission_target, record_target
from app.services.hub_global_search import HubGlobalSearchService


def _user(*, username: str, role: str, team_id: int | None = None) -> HubUser:
    return HubUser(
        username=username,
        password_hash="test-password-hash",
        role=role,
        team_id=team_id,
    )


def test_assigned_and_team_scopes_only_return_their_customer_ids():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        team = access.create_team(name="Vertrieb Süd")
        sales = _user(username="sales", role="sales", team_id=team.id)
        manager = _user(username="manager", role="management", team_id=team.id)
        other = _user(username="other", role="sales")
        first = Customer(name="Erlaubter Kunde")
        second = Customer(name="Fremder Kunde")
        db.add_all((sales, manager, other, first, second))
        db.flush()

        access.assign_record(
            module_key="customers",
            record_id=first.id,
            owner_user_id=sales.id,
            team_id=team.id,
        )
        access.assign_record(
            module_key="customers",
            record_id=second.id,
            owner_user_id=other.id,
            team_id=None,
        )

        assert access.accessible_record_ids(user=sales, module_key="customers") == {first.id}
        assert access.accessible_record_ids(user=manager, module_key="customers") == {first.id}
        assert access.can_access_record(
            user=sales, module_key="customers", record_id=second.id
        ) is False


def test_read_only_grant_does_not_allow_editing_until_upgraded():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        access = HubAccessControlService(db=db)
        access.ensure_defaults()
        access.save_role(
            role_key="restricted",
            name="Eingeschränkt",
            description="Sieht ausschließlich freigegebene Kunden.",
            permissions={
                "customers": {
                    "view": True,
                    "edit": True,
                    "scope": "none",
                }
            },
        )
        user = _user(username="sales", role="restricted")
        customer = Customer(name="Freigegebener Kunde")
        db.add_all((user, customer))
        db.flush()

        access.add_grant(
            module_key="customers",
            record_id=customer.id,
            user_id=user.id,
            team_id=None,
            can_edit=False,
        )
        assert access.can_access_record(
            user=user, module_key="customers", record_id=customer.id, action="view"
        ) is True
        assert access.accessible_record_ids(user=user, module_key="customers") == {customer.id}
        assert access.can_access_record(
            user=user, module_key="customers", record_id=customer.id, action="edit"
        ) is False

        access.add_grant(
            module_key="customers",
            record_id=customer.id,
            user_id=user.id,
            team_id=None,
            can_edit=True,
        )
        assert access.can_access_record(
            user=user, module_key="customers", record_id=customer.id, action="edit"
        ) is True


def test_global_search_omits_customers_outside_the_allowed_id_set():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        visible = Customer(name="Beispiel Sichtbar", website_domain="sichtbar.example")
        hidden = Customer(name="Beispiel Versteckt", website_domain="versteckt.example")
        db.add_all((visible, hidden))
        db.flush()

        groups = HubGlobalSearchService(db=db, cipher=SecretCipher("a" * 32)).search(
            "Beispiel",
            include_admin_modules=False,
            visible_modules={"customers"},
            allowed_record_ids={"customers": {visible.id}},
        )

        assert [item["label"] for item in groups[0]["items"]] == ["Beispiel Sichtbar"]


def test_route_targets_distinguish_create_manage_and_record_access():
    assert permission_target("/calendar/activities", "POST") == ("activities", "create")
    assert permission_target("/customers/new", "GET") == ("customers", "create")
    assert permission_target("/module-layouts/customer-fields", "GET") == ("settings", "manage")
    assert permission_target("/customers/12/fields", "POST") == ("customers", "edit")
    assert record_target("/customers/12/fields") == ("customers", 12)
    assert record_target("/leads/7") == ("leads", 7)
