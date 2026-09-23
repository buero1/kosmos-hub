import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_user import HubUser
from app.models.hub_finance_documents import HubFinanceInvoice
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import (
    DUNNING_MODULE,
    INVOICE_MODULE,
    ORDER_FIELDS_LAYOUT_KEY,
    ORDER_MODULE,
    RECURRING_INVOICE_MODULE,
    HubFinanceDocumentError,
    HubFinanceDocumentService,
)
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.module_layouts import ModuleLayoutService


def _documents(db: Session) -> HubFinanceDocumentService:
    return HubFinanceDocumentService(db=db, cipher=SecretCipher("a" * 32))


def _finance(db: Session) -> HubFinanceService:
    return HubFinanceService(db=db, cipher=SecretCipher("a" * 32))


def _contact_id(db: Session, customer: Customer) -> int:
    contact = db.scalar(select(CustomerContact).where(CustomerContact.customer_id == customer.id))
    if contact is None:
        contact = CustomerContact(
            customer=customer,
            encrypted_profile_json=SecretCipher("a" * 32).encrypt(json.dumps({"fields": {"Name": "Test Kontakt"}})),
        )
        db.add(contact)
        db.flush()
    return contact.id


def test_document_view_uses_description_as_free_text_name_when_name_is_empty():
    assert HubFinanceDocumentService._free_text_values(
        article_id=None,
        name="",
        description="Frei erfasste Zusatzleistung",
    ) == ("Frei erfasste Zusatzleistung", "")


def test_invoice_pages_preserve_date_order_and_limit_entries():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        for number in range(205):
            db.add(HubFinanceInvoice(
                invoice_number=f"RE-{number:06d}",
                encrypted_fields_json=cipher.encrypt(json.dumps({
                    "invoice_date": f"2026-09-{number % 28 + 1:02d}",
                    "status": "draft",
                    "currency": "EUR",
                })),
            ))
        db.commit()

        service = _documents(db)
        full_order = [entry.document.id for entry in service.list_documents(module=INVOICE_MODULE)]
        first = service.list_invoice_page(page=1)
        second = service.list_invoice_page(page=2)
        last = service.list_invoice_page(page=3)
        beyond = service.list_invoice_page(page=999)

        assert (first.page, first.page_count, first.total_count) == (1, 3, 205)
        assert (len(first.entries), len(second.entries), len(last.entries)) == (100, 100, 5)
        assert [entry.document.id for page in (first, second, last) for entry in page.entries] == full_order
        assert (beyond.page, [entry.document.id for entry in beyond.entries]) == (
            3,
            [entry.document.id for entry in last.entries],
        )


def test_invoice_page_handles_empty_list_and_invalid_page_size():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        page = _documents(db).list_invoice_page(page=-4)
        assert (page.entries, page.page, page.page_count, page.total_count) == ((), 1, 1, 0)
        with pytest.raises(ValueError, match="page_size"):
            _documents(db).list_invoice_page(page=1, page_size=0)


def test_customer_document_list_contains_only_the_linked_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)

    with Session(engine) as db:
        first_customer = Customer(name="Erster Kunde")
        second_customer = Customer(name="Zweiter Kunde")
        db.add_all([first_customer, second_customer])
        db.flush()
        first_invoice = HubFinanceInvoice(
            invoice_number="RE-000001",
            customer=first_customer,
            encrypted_fields_json=cipher.encrypt(json.dumps({"invoice_date": "2026-09-11", "status": "draft"})),
        )
        db.add_all([
            first_invoice,
            HubFinanceInvoice(
                invoice_number="RE-000002",
                customer=second_customer,
                encrypted_fields_json=cipher.encrypt(json.dumps({"invoice_date": "2026-09-12", "status": "draft"})),
            ),
        ])
        db.commit()

        entries = _documents(db).list_customer_documents(module=INVOICE_MODULE, customer_id=first_customer.id)

        assert [entry.document.id for entry in entries] == [first_invoice.id]


def _article_values() -> dict[str, str]:
    return {
        "article_field__name": "Jahresbeitrag Homepage",
        "article_field__sku": "JH-1",
        "article_field__status": "active",
        "article_field__kind": "service",
        "article_field__unit": "Jahr",
        "article_field__net_price": "120",
        "article_field__tax_rate": "19",
        "article_field__revenue_account": "8400",
        "article_field__description": "Jährliche Betreuung.",
    }


def _offer_values(*, article_id: int) -> dict[str, str]:
    return {
        "offer_field__status": "draft",
        "offer_field__offer_date": "2026-09-11",
        "offer_field__valid_until": "2026-10-11",
        "offer_field__currency": "EUR",
        "offer_line__0__article_id": str(article_id),
        "offer_line__0__name": "Jahresbeitrag Homepage",
        "offer_line__0__quantity": "1",
        "offer_line__0__unit": "Jährlich",
        "offer_line__0__unit_price": "120",
        "offer_line__0__discount_percent": "0",
        "offer_line__0__tax_rate": "19",
    }


def _document_values(*, module: str, article_id: int) -> dict[str, str]:
    values = {
        "document_field__status": "draft",
        "document_field__currency": "EUR",
        "document_field__payment_terms": "14_days",
        "document_line__0__article_id": str(article_id),
        "document_line__0__name": "Jahresbeitrag Homepage",
        "document_line__0__sku": "JH-1",
        "document_line__0__description": "Jährliche Betreuung.",
        "document_line__0__quantity": "2,00",
        "document_line__0__unit": "Jährlich",
        "document_line__0__unit_price": "120",
        "document_line__0__discount_percent": "10",
        "document_line__0__tax_rate": "19",
    }
    if module == "orders":
        values.update({
            "document_field__order_name": "Homepage-Auftrag",
            "document_field__order_date": "2026-09-11",
            "document_field__contract_term": "12",
            "document_field__payment_method": "Lastschrift",
            "document_field__payment_frequency": "Monatlich",
            "document_field__cancellation_period": "1 Monat vor Ablauf, Verlängerung um 1 Jahr",
            "document_field__order_intake_type": "Schriftlich",
            "document_field__contract_duration_years": "1,50",
        })
    elif module == "invoices":
        values.update({"document_field__invoice_date": "2026-09-11", "document_field__due_date": "2026-09-25"})
    else:
        values.update({
            "document_field__name": "Homepage-Betreuung",
            "document_field__status": "active",
            "document_field__start_date": "2026-09-11",
            "document_field__end_date": "",
            "document_field__interval_unit": "year",
            "document_field__interval_count": "1",
            "document_field__next_invoice_date": "2027-09-11",
            "document_field__automatic_creation": "true",
            "document_field__late_fee": "0",
            "document_field__system_payment_complete": "false",
        })
    return values


def test_orders_and_invoices_keep_positions_and_document_links_locally():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(
            name="Beispiel GmbH",
            encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {
                "Rechnungsadresse - Straße Einzelzeile": "Musterstraße 1",
                "PLZ Ort Hub-Feld": "80331 München",
            }})),
        )
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        offer = _finance(db).create_offer(customer_id=customer.id, contact_id=_contact_id(db, customer), submitted_values=_offer_values(article_id=article.id))
        order = _documents(db).create_document(
            module=ORDER_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=offer.id,
            submitted_values=_document_values(module="orders", article_id=article.id),
        )
        invoice = _documents(db).create_document(
            module=INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=order.id,
            submitted_values=_document_values(module="invoices", article_id=article.id),
        )
        db.commit()

        assert order.order_number == f"AUF-{order.id:06d}"
        assert invoice.invoice_number == f"RE-{invoice.id:06d}"
        assert "Jahresbeitrag" not in invoice.lines[0].encrypted_fields_json

        stored_order_values = json.loads(cipher.decrypt(order.encrypted_fields_json))
        assert stored_order_values["order_name"] == "Homepage-Auftrag"
        assert stored_order_values["contract_duration_years"] == "1.50"
        assert stored_order_values["created_time"]
        assert stored_order_values["modified_time"]
        assert "reference" not in stored_order_values
        assert "currency" not in stored_order_values
        assert "payment_terms" not in stored_order_values

        order_detail = _documents(db).get_detail(module=ORDER_MODULE, document_id=order.id)
        assert order_detail is not None
        assert next(field.value for field in order_detail.fields if field.key == "contract_duration_years") == "1,50"
        assert next(field.value for field in order_detail.fields if field.key == "created_time") != "-"
        assert {field.key for field in order_detail.fields}.isdisjoint({"reference", "currency", "payment_terms"})

        detail = _documents(db).get_detail(module=INVOICE_MODULE, document_id=invoice.id)
        assert detail is not None
        assert detail.link_label == order.order_number
        assert detail.billing_address == "Beispiel GmbH\nMusterstraße 1\n80331 München"
        assert detail.lines[0].amount_net == Decimal("216.00")
        assert detail.lines[0].quantity == "2.00"
        assert detail.totals.total_gross == Decimal("257.04")
        assert next(field.value for field in detail.fields if field.key == "remaining_amount") == "257,04 \u20ac"


def test_dunning_draft_copies_invoice_data_into_an_independent_editable_document():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        contact_id = _contact_id(db, customer)
        article = _finance(db).create_article(submitted_values=_article_values())
        invoice = _documents(db).create_document(
            module=INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=contact_id,
            link_id=None,
            submitted_values=_document_values(module="invoices", article_id=article.id),
        )

        service = _documents(db)
        draft = service.dunning_draft_from_invoice(invoice_id=invoice.id)
        assert draft.invoice_identifier == invoice.invoice_number
        assert (draft.customer_id, draft.contact_id) == (customer.id, contact_id)
        assert draft.submitted_values["document_line__0__name"] == "Jahresbeitrag Homepage"
        draft.submitted_values["document_line__0__name"] = "Mahnposition zur Rechnung"

        dunning = service.create_document(
            module=DUNNING_MODULE,
            customer_id=draft.customer_id,
            contact_id=draft.contact_id,
            link_id=draft.invoice_id,
            submitted_values=draft.submitted_values,
        )
        db.commit()

        assert dunning.dunning_number == f"MAH-{dunning.id:06d}"
        assert dunning.invoice_id == invoice.id
        assert dunning.lines[0].dunning_id == dunning.id
        assert invoice.lines[0].invoice_id == invoice.id
        assert service._values(invoice.lines[0].encrypted_fields_json)["name"] == "Jahresbeitrag Homepage"
        detail = service.get_detail(module=DUNNING_MODULE, document_id=dunning.id)
        assert detail is not None
        assert detail.status == "Zahlungserinnerung"
        assert detail.link_label == invoice.invoice_number
        assert detail.lines[0].name == "Mahnposition zur Rechnung"
        assert dunning.pdf_template.name == "Standardmahnung"


def test_dunning_requires_a_linked_invoice_and_exposes_the_requested_statuses():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        values = _document_values(module="invoices", article_id=article.id)
        values.update({
            "document_field__status": "payment_reminder",
            "document_field__dunning_date": "2026-09-15",
            "document_field__due_date": "2026-09-22",
        })

        with pytest.raises(HubFinanceDocumentError, match="Verknüpfte Rechnung"):
            _documents(db).create_document(
                module=DUNNING_MODULE,
                customer_id=customer.id,
                contact_id=_contact_id(db, customer),
                link_id=None,
                submitted_values=values,
            )

        status = next(field for field in DUNNING_MODULE.fields if field.key == "status")
        assert tuple(label for _, label in status.options) == (
            "Erneuter Abbuchung nach Lastschrift-Retour",
            "Zahlungsaufforderung nach zweiter Lastschrift-Retour",
            "Zahlungserinnerung",
            "Mahnung",
            "Letzte Mahnung",
        )


def test_finance_document_allows_a_position_without_unit():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        values = _document_values(module="invoices", article_id=article.id)
        values.pop("document_line__0__unit")
        invoice = _documents(db).create_document(
            module=INVOICE_MODULE, customer_id=customer.id, contact_id=_contact_id(db, customer),
            link_id=None, submitted_values=values,
        )
        detail = _documents(db).get_detail(module=INVOICE_MODULE, document_id=invoice.id)
        assert detail is not None
        assert detail.lines[0].unit == ""


def test_finance_document_rejects_an_unsupported_position_unit():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        values = _document_values(module="invoices", article_id=article.id)
        values["document_line__0__unit"] = "Wöchentlich"
        with pytest.raises(HubFinanceDocumentError, match="Einheit"):
            _documents(db).create_document(
                module=INVOICE_MODULE,
                customer_id=customer.id,
                contact_id=_contact_id(db, customer),
                link_id=None,
                submitted_values=values,
            )


def test_orders_reject_unknown_new_picklist_values():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        values = _document_values(module="orders", article_id=article.id)
        values["document_field__payment_method"] = "Bar"

        with pytest.raises(HubFinanceDocumentError, match="Zahlungsart"):
            _documents(db).create_document(
                module=ORDER_MODULE,
                customer_id=customer.id,
                contact_id=_contact_id(db, customer),
                link_id=None,
                submitted_values=values,
            )


def test_finance_documents_reject_links_from_another_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_customer = Customer(name="Erster Kunde")
        second_customer = Customer(name="Zweiter Kunde")
        db.add_all((first_customer, second_customer))
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        offer = _finance(db).create_offer(customer_id=second_customer.id, contact_id=_contact_id(db, second_customer), submitted_values=_offer_values(article_id=article.id))

        with pytest.raises(HubFinanceDocumentError, match="verknüpfte Beleg"):
            _documents(db).create_document(
                module=ORDER_MODULE,
                customer_id=first_customer.id,
                contact_id=_contact_id(db, first_customer),
                link_id=offer.id,
                submitted_values=_document_values(module="orders", article_id=article.id),
            )


def test_recurring_invoices_store_schedule_and_follow_the_layout_configuration():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add_all((customer, admin))
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        recurring = _documents(db).create_document(
            module=RECURRING_INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=_document_values(module="recurring", article_id=article.id),
        )
        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=recurring.id)
        assert detail is not None
        assert detail.identifier == "Homepage-Betreuung"
        assert detail.status == "Aktiv"
        assert detail.totals.total_gross == Decimal("257.04")
        hidden_keys = {"interval_count", "automatic_creation", "late_fee", "system_payment_complete"}
        assert {field.key for field in RECURRING_INVOICE_MODULE.fields}.isdisjoint(hidden_keys)
        assert {field.key for field in detail.fields}.isdisjoint(hidden_keys)
        recurring_values = _documents(db)._values(recurring.encrypted_fields_json)
        assert {key: recurring_values[key] for key in hidden_keys} == {
            "interval_count": "1",
            "automatic_creation": "true",
            "late_fee": "0.00",
            "system_payment_complete": "false",
        }

        order = _documents(db).create_document(
            module=ORDER_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=_document_values(module="orders", article_id=article.id),
        )
        order_detail = _documents(db).get_detail(module=ORDER_MODULE, document_id=order.id)
        assert order_detail is not None
        keys = tuple(field.key for field in order_detail.fields)
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=ORDER_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(("customer",) + tuple(key for key in keys if key != "customer")),
            allowed_keys=keys,
        )
        db.commit()

        assert _documents(db).get_detail(module=ORDER_MODULE, document_id=order.id).fields[0].key == "customer"


def test_recurring_invoice_edit_preserves_imported_hidden_values_and_cadence():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        cipher = SecretCipher("a" * 32)
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        submitted = _document_values(module="recurring", article_id=article.id)
        submitted["document_field__interval_unit"] = "quarter"
        recurring = _documents(db).create_document(
            module=RECURRING_INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        imported = _documents(db)._values(recurring.encrypted_fields_json)
        imported.update({
            "interval_count": "2",
            "automatic_creation": "false",
            "late_fee": "9.00",
            "system_payment_complete": "true",
            "zoho_repeat_every": "6",
        })
        recurring.encrypted_fields_json = cipher.encrypt(json.dumps(imported))
        db.flush()

        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=recurring.id)
        assert detail is not None
        cadence = next(field for field in detail.fields if field.key == "interval_unit")
        assert cadence.value == "Alle 2 Quartale"
        assert ("quarter", "Alle 2 Quartale") in cadence.options

        _documents(db).update_document(
            module=RECURRING_INVOICE_MODULE,
            document_id=recurring.id,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        unchanged = _documents(db)._values(recurring.encrypted_fields_json)
        for key in ("interval_count", "automatic_creation", "late_fee", "system_payment_complete", "zoho_repeat_every"):
            assert unchanged[key] == imported[key]

        db.commit()
        db.expire_all()
        submitted["document_field__interval_unit"] = "month"
        _documents(db).update_document(
            module=RECURRING_INVOICE_MODULE,
            document_id=recurring.id,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        changed = _documents(db)._values(recurring.encrypted_fields_json)
        assert changed["interval_unit"] == "month"
        assert changed["interval_count"] == "1"
        assert changed["late_fee"] == "9.00"


def test_recurring_month_start_follows_month_end_and_round_trips():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        submitted = _document_values(module="recurring", article_id=article.id)
        submitted["document_field__interval_unit"] = "month_start"
        document = _documents(db).create_document(
            module=RECURRING_INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )

        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=document.id)
        assert detail is not None
        rhythm = next(field for field in detail.fields if field.key == "interval_unit")
        assert rhythm.value == "Monatsanfang"
        assert rhythm.form_value == "month_start"
        assert rhythm.options.index(("month_start", "Monatsanfang")) == rhythm.options.index(("month_end", "Monatsende")) + 1


def test_recurring_invoice_uses_invoice_pdf_template():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        document = _documents(db).create_document(
            module=RECURRING_INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=_document_values(module="recurring", article_id=article.id),
        )
        assert document.pdf_template.document_type == "invoices"

        order_template = HubPdfTemplateService(db=db).default_for("orders")
        with pytest.raises(HubFinanceDocumentError, match="PDF-Vorlage"):
            _documents(db).update_document(
                module=RECURRING_INVOICE_MODULE,
                document_id=document.id,
                customer_id=customer.id,
                contact_id=_contact_id(db, customer),
                link_id=None,
                submitted_values=_document_values(module="recurring", article_id=article.id),
                pdf_template_id=order_template.id,
            )


def test_finance_documents_require_contact():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())

        for module, name in ((ORDER_MODULE, "orders"), (INVOICE_MODULE, "invoices"), (RECURRING_INVOICE_MODULE, "recurring")):
            with pytest.raises(HubFinanceDocumentError, match="Ansprechpartner ist erforderlich"):
                _documents(db).create_document(
                    module=module,
                    customer_id=customer.id,
                    contact_id=None,
                    link_id=None,
                    submitted_values=_document_values(module=name, article_id=article.id),
                )


@pytest.mark.parametrize(
    ("terms", "due_date"),
    (("due_on_receipt", "2026-09-11"), ("7_days", "2026-09-18"), ("14_days", "2026-09-25"), ("30_days", "2026-10-11")),
)
def test_invoice_payment_goal_calculates_due_date(terms: str, due_date: str):
    with Session(create_engine("sqlite://")) as db:
        submitted = _document_values(module="invoices", article_id=1)
        submitted["document_field__payment_terms"] = terms
        submitted["document_field__due_date"] = ""
        values = _documents(db)._submitted_fields(module=INVOICE_MODULE, submitted_values=submitted)
        assert values["due_date"] == due_date


def test_invoice_payment_goal_preserves_untouched_imported_due_date():
    with Session(create_engine("sqlite://")) as db:
        submitted = _document_values(module="invoices", article_id=1)
        submitted["document_field__due_date"] = "2026-10-01"
        existing = {"invoice_date": "2026-09-11", "payment_terms": "14_days", "due_date": "2026-10-01"}
        assert _documents(db)._submitted_fields(module=INVOICE_MODULE, submitted_values=submitted, existing_values=existing)["due_date"] == "2026-10-01"

        submitted["document_field__invoice_date"] = "2026-09-12"
        assert _documents(db)._submitted_fields(module=INVOICE_MODULE, submitted_values=submitted, existing_values=existing)["due_date"] == "2026-09-26"


def test_invoice_manual_due_date_clears_payment_goal():
    with Session(create_engine("sqlite://")) as db:
        submitted = _document_values(module="invoices", article_id=1)
        existing = {"invoice_date": "2026-09-11", "payment_terms": "14_days", "due_date": "2026-09-25"}
        submitted["document_field__due_date"] = "2026-09-30"
        values = _documents(db)._submitted_fields(module=INVOICE_MODULE, submitted_values=submitted, existing_values=existing)
        assert values["due_date"] == "2026-09-30"
        assert values["payment_terms"] == ""

        submitted["document_field__due_date"] = "2026-09-25"
        values = _documents(db)._submitted_fields(module=INVOICE_MODULE, submitted_values=submitted, existing_values=existing)
        assert values["payment_terms"] == "14_days"


def test_invoice_manual_due_date_on_create_clears_payment_goal():
    with Session(create_engine("sqlite://")) as db:
        submitted = _document_values(module="invoices", article_id=1)
        submitted["document_field__due_date"] = "2026-09-30"
        values = _documents(db)._submitted_fields(module=INVOICE_MODULE, submitted_values=submitted)
        assert values["due_date"] == "2026-09-30"
        assert values["payment_terms"] == ""


@pytest.mark.parametrize(
    ("unit", "count", "expected"),
    (("day", "1", "1 Tag"), ("week", "2", "2 Wochen"), ("month", "1", "1 Monat"), ("month", "2", "2 Monate"), ("year", "3", "3 Jahre")),
)
def test_recurring_custom_rhythm_stores_and_displays_both_parts(unit: str, count: str, expected: str):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        submitted = _document_values(module="recurring", article_id=article.id)
        submitted.update({
            "document_field__interval_unit": "custom",
            "document_field__custom_interval_count": count,
            "document_field__custom_interval_unit": unit,
        })
        recurring = _documents(db).create_document(
            module=RECURRING_INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        stored = _documents(db)._values(recurring.encrypted_fields_json)
        assert stored["interval_unit"] == "custom"
        assert stored["custom_interval_count"] == count
        assert stored["custom_interval_unit"] == unit

        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=recurring.id)
        assert detail is not None
        custom = next(field for field in detail.fields if field.key == "custom_interval")
        assert custom.value == expected
        assert custom.form_value == f"{count}:{unit}"

        submitted["document_field__interval_unit"] = "month_end"
        submitted.pop("document_field__custom_interval_count")
        submitted.pop("document_field__custom_interval_unit")
        _documents(db).update_document(
            module=RECURRING_INVOICE_MODULE,
            document_id=recurring.id,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        changed = _documents(db)._values(recurring.encrypted_fields_json)
        assert changed["interval_unit"] == "month_end"
        assert changed["custom_interval_count"] == ""
        assert changed["custom_interval_unit"] == ""
        assert changed["interval_count"] == "1"
        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=recurring.id)
        assert detail is not None
        assert next(field.value for field in detail.fields if field.key == "interval_unit") == "Monatsende"
        assert next(field.value for field in detail.fields if field.key == "custom_interval") == ""


@pytest.mark.parametrize(
    ("count", "unit", "message"),
    (("", "day", "erforderlich"), ("0", "day", "ungültig"), ("10000", "week", "ungültig"), ("2", "hour", "ungültig")),
)
def test_recurring_custom_rhythm_rejects_incomplete_or_invalid_parts(count: str, unit: str, message: str):
    engine = create_engine("sqlite://")
    with Session(engine) as db:
        service = _documents(db)
        submitted = {
            "document_field__name": "Beispiel",
            "document_field__status": "active",
            "document_field__start_date": "2026-09-13",
            "document_field__interval_unit": "custom",
            "document_field__next_invoice_date": "2026-09-20",
            "document_field__currency": "EUR",
            "document_field__custom_interval_count": count,
            "document_field__custom_interval_unit": unit,
        }
        with pytest.raises(HubFinanceDocumentError, match=message):
            service._submitted_fields(module=RECURRING_INVOICE_MODULE, submitted_values=submitted)


def test_recurring_payment_due_replaces_only_this_modules_payment_terms():
    assert "payment_terms" in {field.key for field in INVOICE_MODULE.fields}
    recurring_keys = {field.key for field in RECURRING_INVOICE_MODULE.fields}
    assert "payment_due" in recurring_keys
    assert "payment_terms" not in recurring_keys

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        submitted = _document_values(module="recurring", article_id=article.id)
        submitted.update({
            "document_field__payment_due_count": "2",
            "document_field__payment_due_unit": "week",
        })
        recurring = _documents(db).create_document(
            module=RECURRING_INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        stored = _documents(db)._values(recurring.encrypted_fields_json)
        assert stored["payment_due_count"] == "2"
        assert stored["payment_due_unit"] == "week"
        assert stored["payment_terms"] == ""

        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=recurring.id)
        assert detail is not None
        due = next(field for field in detail.fields if field.key == "payment_due")
        assert due.label == "Zahlungsziel"
        assert due.value == "2 Wochen"
        assert due.form_value == "2:week"

        submitted["document_field__payment_due_count"] = "0"
        submitted["document_field__payment_due_unit"] = "day"
        _documents(db).update_document(
            module=RECURRING_INVOICE_MODULE,
            document_id=recurring.id,
            customer_id=customer.id,
            contact_id=_contact_id(db, customer),
            link_id=None,
            submitted_values=submitted,
        )
        changed = _documents(db)._values(recurring.encrypted_fields_json)
        assert changed["payment_due_count"] == "0"
        assert changed["payment_due_unit"] == "day"
        assert changed["payment_terms"] == "due_on_receipt"


def test_recurring_payment_due_reads_legacy_zoho_days_without_migration():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = _documents(db)
        legacy = service._field_values(
            module=RECURRING_INVOICE_MODULE,
            values={"interval_unit": "year", "payment_terms": "7_days"},
        )
        due = next(field for field in legacy if field.key == "payment_due")
        assert due.value == "7 Tage"
        assert due.form_value == "7:day"
        empty = service._field_values(module=RECURRING_INVOICE_MODULE, values={"interval_unit": "year"})
        assert next(field.value for field in empty if field.key == "payment_due") == ""


@pytest.mark.parametrize(
    ("count", "unit"),
    (("", "day"), ("2", ""), ("-1", "day"), ("10000", "year"), ("3", "month")),
)
def test_recurring_payment_due_requires_valid_paired_values(count: str, unit: str):
    engine = create_engine("sqlite://")
    with Session(engine) as db:
        submitted = {
            "document_field__name": "Beispiel",
            "document_field__status": "active",
            "document_field__start_date": "2026-09-13",
            "document_field__interval_unit": "month",
            "document_field__next_invoice_date": "2026-10-13",
            "document_field__currency": "EUR",
            "document_field__payment_due_count": count,
            "document_field__payment_due_unit": unit,
        }
        with pytest.raises(HubFinanceDocumentError, match="Zahlungsziel"):
            _documents(db)._submitted_fields(module=RECURRING_INVOICE_MODULE, submitted_values=submitted)
