from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes import web
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.customer_directory import CONTACT_FIELDS_LAYOUT_KEY, CUSTOMER_FIELDS_LAYOUT_KEY, HUB_CUSTOMER_FIELD_KEYS
from app.services.hub_case_field_catalog import HUB_CASE_FIELDS
from app.services.hub_cases import CASE_FIELDS_LAYOUT_KEY
from app.services.hub_finance import ARTICLE_FIELDS_LAYOUT_KEY, OFFER_FIELDS_LAYOUT_KEY
from app.services.hub_finance_documents import FINANCE_DOCUMENT_MODULES
from app.services.hub_finance_field_catalog import ARTICLE_FIELDS, OFFER_FIELDS
from app.services.hub_lead_field_catalog import HUB_LEAD_FIELDS
from app.services.hub_leads import LEAD_FIELDS_LAYOUT_KEY
from app.services.module_layouts import ModuleLayoutService
from app.services.styling_settings import StylingRuntimeSettings
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "session": {},
    })


def _reverse_layout(db: Session, actor: HubUser, layout_key: str, fields: tuple[object, ...]) -> tuple[str, ...]:
    keys = tuple(field.key for field in fields)
    reversed_keys = tuple(reversed(keys))
    ModuleLayoutService(db=db).configure(
        actor=actor,
        layout_key=layout_key,
        item_order_json="[" + ",".join(f'"{key}"' for key in reversed_keys) + "]",
        allowed_keys=keys,
    )
    return reversed_keys


def test_create_forms_use_the_saved_layout_order():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(actor)
        db.flush()
        request = _request()

        lead_order = _reverse_layout(db, actor, LEAD_FIELDS_LAYOUT_KEY, HUB_LEAD_FIELDS)
        assert tuple(field.key for field in web._lead_create_context(request, db)["fields"]) == lead_order

        contact_order = _reverse_layout(db, actor, CONTACT_FIELDS_LAYOUT_KEY, ZOHO_CONTACT_FIELDS)
        assert tuple(field.key for field in web._contact_create_context(request, db)["fields"]) == contact_order

        case_order = _reverse_layout(db, actor, CASE_FIELDS_LAYOUT_KEY, HUB_CASE_FIELDS)
        assert tuple(field.key for field in web._case_create_context(request, db)["fields"]) == case_order

        article_order = _reverse_layout(db, actor, ARTICLE_FIELDS_LAYOUT_KEY, ARTICLE_FIELDS)
        assert tuple(field.key for field in web._finance_article_create_context(request, db)["fields"]) == article_order

        offer_order = _reverse_layout(db, actor, OFFER_FIELDS_LAYOUT_KEY, OFFER_FIELDS)
        assert tuple(field.key for field in web._finance_offer_create_context(request, db)["fields"]) == offer_order

        for module in FINANCE_DOCUMENT_MODULES.values():
            module_order = _reverse_layout(db, actor, module.layout_key, module.fields)
            context = web._finance_document_create_context(request, db, module=module)
            assert tuple(field.key for field in context["fields"]) == module_order


def test_finance_and_case_create_contexts_keep_a_preselected_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        request = _request()
        customer_id = 73
        customer = Customer(id=customer_id, name="Verknüpfter Testkunde")
        db.add(customer)
        db.flush()

        case_context = web._case_create_context(request, db, selected_customer_id=customer_id)
        assert case_context["selected_customer_id"] == customer_id
        assert case_context["selected_customer"] is customer

        offer_context = web._finance_offer_create_context(request, db, selected_customer_id=customer_id)
        assert offer_context["selected_customer_id"] == customer_id
        assert offer_context["selected_customer"] is customer
        for module in FINANCE_DOCUMENT_MODULES.values():
            context = web._finance_document_create_context(
                request,
                db,
                module=module,
                selected_customer_id=customer_id,
            )
            assert context["selected_customer_id"] == customer_id
            assert context["selected_customer"] is customer


def test_related_create_forms_show_the_customer_link_panel():
    templates = (
        "case_create.html",
        "finance_offer_create.html",
        "finance_document_create.html",
    )

    for template_name in templates:
        template = web.templates.env.get_template(template_name)
        source = template.environment.loader.get_source(template.environment, template_name)[0]
        assert "create_customer_link_panel" in source
        assert 'partials/create_customer_link_script.html' in source


def test_invoice_detail_offers_dunning_creation_as_the_first_menu_action():
    template = web.templates.env.get_template("finance_document_detail.html")
    source = template.environment.loader.get_source(template.environment, template.name)[0]

    menu_start = source.index('class="detail-actions-menu-popover"')
    dunning_action = source.index("Mahnung anlegen", menu_start)
    delete_action = source.index("löschen", menu_start)
    assert '/finance/dunnings/new?invoice_id={{ detail.document.id }}' in source
    assert dunning_action < delete_action


def test_customer_create_form_uses_the_saved_relative_layout_order():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(actor)
        db.flush()
        reversed_keys = tuple(reversed(HUB_CUSTOMER_FIELD_KEYS))
        ModuleLayoutService(db=db).configure(
            actor=actor,
            layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
            item_order_json="[" + ",".join(f'"{key}"' for key in reversed_keys) + "]",
            allowed_keys=HUB_CUSTOMER_FIELD_KEYS,
        )

        context = web._hub_customer_create_context(_request(), db)

        assert context["field_order"] == reversed_keys
        template = web.templates.env.get_template("customer_create.html")
        assert template is not None


def test_customer_create_form_expands_the_saved_postal_city_field_in_place():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        actor = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(actor)
        db.flush()
        ModuleLayoutService(db=db).configure(
            actor=actor,
            layout_key=CUSTOMER_FIELDS_LAYOUT_KEY,
            item_order_json='["customer_name","hub_postal_city"]',
            allowed_keys=("customer_name", "hub_postal_city"),
        )

        order = web._hub_customer_create_context(_request(), db)["field_order"]

        assert order[:3] == ("customer_name", "billing_postal_code", "billing_city")
        assert set(order) == set(HUB_CUSTOMER_FIELD_KEYS)


def test_new_lead_page_renders_the_salutation_picker(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web.templates, "context_processors", [lambda request: {
        "styling": StylingRuntimeSettings(),
        "unread_email_count": 0,
        "can_launch_wordpress_admin": False,
        "can_use_global_email_composer": False,
        "email_composer_settings": None,
        "email_ai_prompt_presets": (),
        "agent_page_context": None,
    }])

    with Session(engine) as db:
        actor = HubUser(username="hub-admin", password_hash="hash", role="admin")
        db.add(actor)
        db.flush()
        request = _request()
        request.state.hub_user = actor

        html = web.new_lead_page(request, db).body.decode("utf-8")

        start = html.index('name="lead_field__salutation"')
        end = html.index("</select>", start)
        salutation_select = html[start:end]
        assert salutation_select.index(">Frau<") < salutation_select.index(">Herr<")
        assert salutation_select.index(">Herr<") < salutation_select.index(">Frau Dr.<")
        assert salutation_select.index(">Frau Dr.<") < salutation_select.index(">Herr Dr.<")
