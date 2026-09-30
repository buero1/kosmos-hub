from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.core.templates import create_templates
from app.models.customer import Customer
from app.models.customer_checklist import CustomerChecklist
from app.models.hub_user import HubUser
from app.services.customer_checklists import (
    CUSTOMER_CHECKLIST_TEMPLATE_VERSION,
    DEFAULT_DESIGN_CHECKLIST,
    DEFAULT_FINAL_SETUP_CHECKLIST,
    DEFAULT_POST_REVIEW_CHECKLIST,
    CustomerChecklistError,
    CustomerChecklistService,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def customer(db, name="Example"):
    value = Customer(name=name)
    db.add(value)
    db.flush()
    return value


def test_default_customer_checklists_are_initialized_once(db):
    first = customer(db, "First")
    second = customer(db, "Second")
    service = CustomerChecklistService(db=db)

    assert service.initialize_pending_customers() == 2
    assert service.initialize_pending_customers() == 0
    views = service.list_for_customer(customer_id=first.id)

    assert [view.title for view in views] == ["Design Seiten", "Nach Kundensicht", "Letzte Einrichtungen"]
    assert [item.text for item in views[0].items] == list(DEFAULT_DESIGN_CHECKLIST)
    assert [item.text for item in views[1].items] == list(DEFAULT_POST_REVIEW_CHECKLIST)
    assert [item.text for item in views[2].items] == list(DEFAULT_FINAL_SETUP_CHECKLIST)
    assert [len(view.items) for view in service.list_for_customer(customer_id=second.id)] == [9, 6, 18]
    assert first.checklists_template_version == CUSTOMER_CHECKLIST_TEMPLATE_VERSION


def test_existing_design_checklist_receives_only_new_template_sections(db):
    owner = customer(db)
    owner.checklists_initialized = True
    owner.checklists_template_version = 0
    design = CustomerChecklist(customer_id=owner.id, title="Design Seiten", sort_order=1)
    db.add(design)
    db.flush()
    service = CustomerChecklistService(db=db)

    assert service.initialize_pending_customers() == 1
    assert [view.title for view in service.list_for_customer(customer_id=owner.id)] == [
        "Design Seiten",
        "Nach Kundensicht",
        "Letzte Einrichtungen",
    ]
    assert service.initialize_pending_customers() == 0


def test_customer_checklists_support_crud_toggle_and_completion(db):
    owner = customer(db)
    service = CustomerChecklistService(db=db)
    service.initialize_customer(owner)
    checklist = service.list_for_customer(customer_id=owner.id)[0]

    renamed = service.update_checklist(customer_id=owner.id, checklist_id=checklist.id, title="  Design fertig  ")
    assert renamed.title == "Design fertig"
    created = service.create_item(customer_id=owner.id, checklist_id=checklist.id, text="  Letzter Test  ")
    service.update_item(customer_id=owner.id, checklist_id=checklist.id, item_id=created.id, text="Letzte Prüfung")
    service.delete_item(customer_id=owner.id, checklist_id=checklist.id, item_id=created.id)

    for item in service.list_for_customer(customer_id=owner.id)[0].items:
        service.toggle_item(
            customer_id=owner.id,
            checklist_id=checklist.id,
            item_id=item.id,
            actor="admin",
        )
    completed = service.list_for_customer(customer_id=owner.id)[0]
    assert completed.is_completed is True
    assert all(item.is_completed for item in completed.items)

    service.toggle_item(
        customer_id=owner.id,
        checklist_id=checklist.id,
        item_id=completed.items[0].id,
        actor="admin",
    )
    assert service.list_for_customer(customer_id=owner.id)[0].is_completed is False


def test_checklist_and_item_order_are_scoped_to_customer(db):
    owner = customer(db, "Owner")
    other = customer(db, "Other")
    service = CustomerChecklistService(db=db)
    service.initialize_customer(owner)
    service.initialize_customer(other)
    defaults = service.list_for_customer(customer_id=owner.id)
    first = defaults[0]
    second = service.create_checklist(customer_id=owner.id, title="Launch")

    reordered = (second.id, *(view.id for view in defaults))
    service.reorder_checklists(customer_id=owner.id, ordered_ids=reordered)
    assert [view.id for view in service.list_for_customer(customer_id=owner.id)] == list(reordered)

    items = service.list_for_customer(customer_id=owner.id)[1].items
    reversed_ids = tuple(item.id for item in reversed(items))
    service.reorder_items(customer_id=owner.id, checklist_id=first.id, ordered_ids=reversed_ids)
    assert [item.id for item in service.list_for_customer(customer_id=owner.id)[1].items] == list(reversed_ids)

    with pytest.raises(CustomerChecklistError):
        service.update_checklist(
            customer_id=other.id,
            checklist_id=first.id,
            title="Wrong customer",
        )
    with pytest.raises(CustomerChecklistError):
        service.reorder_checklists(customer_id=owner.id, ordered_ids=(first.id,))


def test_deleted_last_checklist_is_not_recreated(db):
    owner = customer(db)
    service = CustomerChecklistService(db=db)
    service.initialize_customer(owner)
    for checklist in service.list_for_customer(customer_id=owner.id):
        service.delete_checklist(customer_id=owner.id, checklist_id=checklist.id)

    assert service.initialize_pending_customers() == 0
    assert service.list_for_customer(customer_id=owner.id) == ()


def test_customer_checklist_ui_contract():
    route = Path("app/api/routes/web.py").read_text(encoding="utf-8")
    detail = Path("app/templates/customer_detail.html").read_text(encoding="utf-8")
    partial = Path("app/templates/partials/customer_checklists.html").read_text(encoding="utf-8")
    script = Path("app/static/customer-checklists.js").read_text(encoding="utf-8")
    styles = Path("app/templates/base.html").read_text(encoding="utf-8")
    main = Path("app/main.py").read_text(encoding="utf-8")

    assert detail.index("customer-fields-tab-{{ tab.key }}") < detail.index("customer-fields-tab-checklists")
    assert '"customer_id": customer_id' in route
    assert 'data-field-tab-panel="checklists"' in detail
    assert 'data-checklist-form-action="checklist.create"' in partial
    assert 'data-checklist-form-action="item.create"' in partial
    assert "data-checklist-item-toggle" in partial
    assert "customer-checklist-item-drag" in partial and "customer-checklist-drag" in partial
    assert script.count("window.confirm") == 1
    assert 'submit("item.delete"' in script
    assert 'item.classList.toggle("is-completed", completed)' in script
    assert "7/9" not in partial and "Fortschritt" not in partial
    assert ".customer-checklist-panel[hidden] { display: none; }" in styles
    customer_schema = main[main.index('if "customers" in table_names:'):]
    assert '"checklists_initialized": "TINYINT(1) NOT NULL DEFAULT 0"' in customer_schema
    assert '"checklists_template_version": "INT NOT NULL DEFAULT 0"' in customer_schema
    case_schema = main[main.index('if "hub_cases" not in table_names:'):main.index('if "hub_email_template_folders" not in table_names:')]
    assert "checklists_initialized" not in case_schema


def test_customer_checklist_partial_renders_editable_items():
    templates = create_templates(directory="app/templates")
    html = templates.env.get_template("partials/customer_checklists.html").render(
        customer_id=42,
        customer_checklists=(
            type("Checklist", (), {
                "id": 7,
                "title": "Design Seiten",
                "items": (type("Item", (), {"id": 9, "text": "Browsertests", "is_completed": True})(),),
                "is_completed": True,
            })(),
        ),
        can_manage_checklists=True,
        csrf_token="csrf",
    )
    assert 'data-customer-checklists-url="/customers/42/checklists/actions"' in html
    assert 'data-checklist-id="7"' in html
    assert 'data-checklist-item-id="9"' in html
    assert "Browsertests" in html and "is-completed" in html
    assert 'aria-expanded="false"' in html
    assert 'data-checklist-panel hidden' in html


def test_checklist_action_endpoint_requires_csrf_and_updates_exact_customer():
    from app.api.routes import web

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = HubUser(username="admin", password_hash="x", role="admin", is_active=True)
        owner = Customer(name="Owner")
        other = Customer(name="Other")
        db.add_all([user, owner, other])
        db.flush()
        service = CustomerChecklistService(db=db)
        service.initialize_customer(owner)
        service.initialize_customer(other)
        db.commit()
        checklist = service.list_for_customer(customer_id=owner.id)[0]
        item = checklist.items[0]

        app = FastAPI()
        app.add_api_route(
            "/customers/{customer_id}/checklists/actions",
            web.update_customer_checklists,
            methods=["POST"],
        )
        app.dependency_overrides[web.get_db] = lambda: db

        @app.middleware("http")
        async def auth(request, call_next):
            request.state.hub_user = user
            request.scope["session"] = {"csrf_token": "csrf-test"}
            return await call_next(request)

        with TestClient(app) as client:
            response = client.post(
                f"/customers/{owner.id}/checklists/actions",
                data={
                    "csrf_token": "csrf-test",
                    "action": "item.toggle",
                    "checklist_id": checklist.id,
                    "item_id": item.id,
                    "active_id": checklist.id,
                },
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            assert "is-completed" in response.json()["html"]
            assert service.list_for_customer(customer_id=owner.id)[0].items[0].is_completed is True
            assert service.list_for_customer(customer_id=other.id)[0].items[0].is_completed is False

            rejected = client.post(
                f"/customers/{owner.id}/checklists/actions",
                data={"csrf_token": "wrong", "action": "checklist.create", "title": "No"},
            )
            assert rejected.status_code == 403
    engine.dispose()
