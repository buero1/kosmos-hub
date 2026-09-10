import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.hub_user import HubUser
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_documents import (
    INVOICE_MODULE,
    ORDER_FIELDS_LAYOUT_KEY,
    ORDER_MODULE,
    RECURRING_INVOICE_MODULE,
    HubFinanceDocumentError,
    HubFinanceDocumentService,
)
from app.services.module_layouts import ModuleLayoutService


def _documents(db: Session) -> HubFinanceDocumentService:
    return HubFinanceDocumentService(db=db, cipher=SecretCipher("a" * 32))


def _finance(db: Session) -> HubFinanceService:
    return HubFinanceService(db=db, cipher=SecretCipher("a" * 32))


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
        "document_line__0__quantity": "2",
        "document_line__0__unit": "Jahr",
        "document_line__0__unit_price": "120",
        "document_line__0__discount_percent": "10",
        "document_line__0__tax_rate": "19",
    }
    if module == "orders":
        values.update({"document_field__order_date": "2026-09-11", "document_field__reference": "Homepage"})
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
        offer = _finance(db).create_offer(customer_id=customer.id, contact_id=None, submitted_values=_offer_values(article_id=article.id))
        order = _documents(db).create_document(
            module=ORDER_MODULE,
            customer_id=customer.id,
            contact_id=None,
            link_id=offer.id,
            submitted_values=_document_values(module="orders", article_id=article.id),
        )
        invoice = _documents(db).create_document(
            module=INVOICE_MODULE,
            customer_id=customer.id,
            contact_id=None,
            link_id=order.id,
            submitted_values=_document_values(module="invoices", article_id=article.id),
        )
        db.commit()

        assert order.order_number == f"AUF-{order.id:06d}"
        assert invoice.invoice_number == f"RE-{invoice.id:06d}"
        assert "Jahresbeitrag" not in invoice.lines[0].encrypted_fields_json

        detail = _documents(db).get_detail(module=INVOICE_MODULE, document_id=invoice.id)
        assert detail is not None
        assert detail.link_label == order.order_number
        assert detail.billing_address == "Beispiel GmbH\nMusterstraße 1\n80331 München"
        assert detail.lines[0].amount_net == Decimal("216.00")
        assert detail.totals.total_gross == Decimal("257.04")
        assert next(field.value for field in detail.fields if field.key == "remaining_amount") == "257,04 EUR"


def test_finance_documents_reject_links_from_another_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_customer = Customer(name="Erster Kunde")
        second_customer = Customer(name="Zweiter Kunde")
        db.add_all((first_customer, second_customer))
        db.flush()
        article = _finance(db).create_article(submitted_values=_article_values())
        offer = _finance(db).create_offer(customer_id=second_customer.id, contact_id=None, submitted_values=_offer_values(article_id=article.id))

        with pytest.raises(HubFinanceDocumentError, match="verknüpfte Beleg"):
            _documents(db).create_document(
                module=ORDER_MODULE,
                customer_id=first_customer.id,
                contact_id=None,
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
            contact_id=None,
            link_id=None,
            submitted_values=_document_values(module="recurring", article_id=article.id),
        )
        detail = _documents(db).get_detail(module=RECURRING_INVOICE_MODULE, document_id=recurring.id)
        assert detail is not None
        assert detail.identifier == "Homepage-Betreuung"
        assert detail.status == "Aktiv"
        assert detail.totals.total_gross == Decimal("257.04")

        order = _documents(db).create_document(
            module=ORDER_MODULE,
            customer_id=customer.id,
            contact_id=None,
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
