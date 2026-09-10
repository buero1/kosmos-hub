import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_user import HubUser
from app.services.hub_finance import (
    ARTICLE_FIELDS_LAYOUT_KEY,
    OFFER_FIELDS_LAYOUT_KEY,
    HubFinanceError,
    HubFinanceService,
)
from app.services.module_layouts import ModuleLayoutService


def _service(db: Session) -> HubFinanceService:
    return HubFinanceService(db=db, cipher=SecretCipher("a" * 32))


def _article_values(**overrides: str) -> dict[str, str]:
    values = {
        "article_field__name": "Monatsbeitrag Homepage",
        "article_field__sku": "260",
        "article_field__status": "active",
        "article_field__kind": "service",
        "article_field__unit": "Monat",
        "article_field__net_price": "49,99",
        "article_field__tax_rate": "19",
        "article_field__revenue_account": "8400",
        "article_field__description": "Monatliche Betreuung der Homepage.",
    }
    values.update(overrides)
    return values


def _offer_values(*, article_id: int, **overrides: str) -> dict[str, str]:
    values = {
        "offer_field__status": "draft",
        "offer_field__offer_date": "2026-09-11",
        "offer_field__valid_until": "2026-10-11",
        "offer_field__currency": "EUR",
        "offer_field__payment_terms": "14_days",
        "offer_field__reference": "Projekt Homepage",
        "offer_line__0__article_id": str(article_id),
        "offer_line__0__name": "Monatsbeitrag Homepage",
        "offer_line__0__sku": "260",
        "offer_line__0__description": "Monatliche Betreuung der Homepage.",
        "offer_line__0__quantity": "2",
        "offer_line__0__unit": "Monat",
        "offer_line__0__unit_price": "49.99",
        "offer_line__0__discount_percent": "10",
        "offer_line__0__tax_rate": "19",
    }
    values.update(overrides)
    return values


def test_finance_article_uses_the_reviewed_catalog_and_keeps_data_encrypted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        article = _service(db).create_article(submitted_values=_article_values())
        db.commit()

        assert "Monatsbeitrag" not in article.encrypted_fields_json
        stored = json.loads(SecretCipher("a" * 32).decrypt(article.encrypted_fields_json))
        assert stored["net_price"] == "49.99"

        detail = _service(db).get_article_detail(article_id=article.id)
        assert detail is not None
        assert detail.name == "Monatsbeitrag Homepage"
        assert next(field.value for field in detail.fields if field.key == "tax_rate") == "19 %"
        assert next(field.value for field in detail.fields if field.key == "net_price") == "49,99 EUR"


def test_finance_offer_calculates_totals_and_snapshots_its_position_values():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        article = _service(db).create_article(submitted_values=_article_values())
        offer = _service(db).create_offer(
            customer_id=customer.id,
            contact_id=None,
            submitted_values=_offer_values(article_id=article.id),
        )
        db.commit()

        assert offer.offer_number == f"ANG-{offer.id:06d}"
        assert "Monatsbeitrag" not in offer.lines[0].encrypted_fields_json

        detail = _service(db).get_offer_detail(offer_id=offer.id)
        assert detail is not None
        assert detail.status == "Entwurf"
        assert detail.fields[0].value == offer.offer_number
        assert len(detail.lines) == 1
        assert detail.lines[0].amount_net == Decimal("89.98")
        assert detail.totals.subtotal_net == Decimal("99.98")
        assert detail.totals.discount_total == Decimal("10.00")
        assert detail.totals.tax_total == Decimal("17.10")
        assert detail.totals.total_gross == Decimal("107.08")
        assert _service(db).list_offers()[0].total_gross == "107,08 EUR"


def test_finance_offer_rejects_a_contact_of_another_customer():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first_customer = Customer(name="Erster Kunde")
        second_customer = Customer(name="Zweiter Kunde")
        contact = CustomerContact(
            customer=second_customer,
            encrypted_profile_json=SecretCipher("a" * 32).encrypt(json.dumps({"fields": {"Name": "Mara Beispiel"}})),
        )
        db.add_all((first_customer, second_customer, contact))
        db.flush()
        article = _service(db).create_article(submitted_values=_article_values())

        with pytest.raises(HubFinanceError, match="Ansprechpartner"):
            _service(db).create_offer(
                customer_id=first_customer.id,
                contact_id=contact.id,
                submitted_values=_offer_values(article_id=article.id),
            )


def test_finance_fields_follow_the_global_layout_configuration():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        admin = HubUser(username="operator", password_hash="hashed", role="admin")
        db.add(admin)
        db.flush()
        article = _service(db).create_article(submitted_values=_article_values())
        customer = Customer(name="Beispiel GmbH")
        db.add(customer)
        db.flush()
        offer = _service(db).create_offer(
            customer_id=customer.id,
            contact_id=None,
            submitted_values=_offer_values(article_id=article.id),
        )
        db.commit()

        article_detail = _service(db).get_article_detail(article_id=article.id)
        offer_detail = _service(db).get_offer_detail(offer_id=offer.id)
        assert article_detail is not None and offer_detail is not None
        article_keys = tuple(field.key for field in article_detail.fields)
        offer_keys = tuple(field.key for field in offer_detail.fields)
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=ARTICLE_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(("net_price",) + tuple(key for key in article_keys if key != "net_price")),
            allowed_keys=article_keys,
        )
        ModuleLayoutService(db=db).configure(
            actor=admin,
            layout_key=OFFER_FIELDS_LAYOUT_KEY,
            item_order_json=json.dumps(("customer",) + tuple(key for key in offer_keys if key != "customer")),
            allowed_keys=offer_keys,
        )
        db.commit()

        assert _service(db).get_article_detail(article_id=article.id).fields[0].key == "net_price"
        assert _service(db).get_offer_detail(offer_id=offer.id).fields[0].key == "customer"
