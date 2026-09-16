import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.models.hub_finance_documents import HubFinanceInvoice, HubFinanceRecurringInvoice, HubFinanceRecurringInvoiceLine
from app.models.hub_finance_generated_pdf import HubFinanceGeneratedPdf
from app.services.hub_recurring_invoice_generation import HubRecurringInvoiceGenerationService, _due_date, next_recurring_date


def _encrypt(cipher: SecretCipher, values: dict[str, str]) -> str:
    return cipher.encrypt(json.dumps(values))


def _recurring(db: Session, cipher: SecretCipher, *, contact: bool = True) -> HubFinanceRecurringInvoice:
    customer = Customer(name="Beispiel GmbH")
    db.add(customer)
    db.flush()
    person = CustomerContact(
        customer=customer,
        encrypted_profile_json=_encrypt(cipher, {"fields": {"Name": "Erika Beispiel"}}),
    ) if contact else None
    if person is not None:
        db.add(person)
        db.flush()
    recurring = HubFinanceRecurringInvoice(
        customer=customer,
        contact=person,
        hub_next_run_on=date(2026, 9, 25),
        encrypted_fields_json=_encrypt(cipher, {
            "name": "Website Paket Basic",
            "status": "active",
            "start_date": "2026-01-25",
            "end_date": "",
            "interval_unit": "month",
            "interval_count": "1",
            "next_invoice_date": "2026-09-25",
            "currency": "EUR",
            "payment_terms": "7_days",
        }),
    )
    recurring.lines.append(HubFinanceRecurringInvoiceLine(
        position_index=0,
        encrypted_fields_json=_encrypt(cipher, {
            "name": "Website-Betreuung",
            "description": "Monatliche Pflege",
            "quantity": "2.00",
            "unit": "Monatlich",
            "unit_price": "49.00",
            "discount_percent": "0.00",
            "tax_rate": "19",
        }),
    ))
    db.add(recurring)
    db.commit()
    return recurring


@pytest.mark.parametrize(("current", "values", "expected"), [
    (date(2026, 1, 31), {"start_date": "2026-01-31", "interval_unit": "month"}, date(2026, 2, 28)),
    (date(2026, 2, 28), {"start_date": "2026-01-31", "interval_unit": "month"}, date(2026, 3, 31)),
    (date(2026, 1, 31), {"start_date": "2026-01-31", "interval_unit": "month_end"}, date(2026, 2, 28)),
    (date(2026, 2, 1), {"start_date": "2026-01-15", "interval_unit": "month_start"}, date(2026, 3, 1)),
    (date(2026, 9, 25), {"start_date": "2020-09-25", "interval_unit": "quarter", "interval_count": "2"}, date(2027, 3, 25)),
    (date(2026, 9, 25), {"start_date": "2020-09-25", "interval_unit": "custom", "custom_interval_count": "2", "custom_interval_unit": "week"}, date(2026, 10, 9)),
    (date(2024, 2, 29), {"start_date": "2024-02-29", "interval_unit": "year"}, date(2025, 2, 28)),
    (date(2025, 2, 28), {"start_date": "2024-02-29", "interval_unit": "year"}, date(2026, 2, 28)),
])
def test_next_recurring_date(current, values, expected):
    assert next_recurring_date(current, values) == expected


def test_custom_payment_due_dates_keep_manual_invoice_due_date():
    assert _due_date(date(2026, 9, 25), {"payment_due_count": "2", "payment_due_unit": "week"}) == (date(2026, 10, 9), "")
    assert _due_date(date(2024, 2, 29), {"payment_due_count": "1", "payment_due_unit": "year"}) == (date(2025, 2, 28), "")


def test_due_recurring_invoice_creates_one_draft_and_queues_pdf():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        recurring = _recurring(db, cipher)
        service = HubRecurringInvoiceGenerationService(db=db, cipher=cipher)
        assert service.create_due(today=date(2026, 9, 24)).created_ids == ()

        result = service.create_due(today=date(2026, 9, 25))
        assert len(result.created_ids) == 1
        invoice = db.get(HubFinanceInvoice, result.created_ids[0])
        values = json.loads(cipher.decrypt(invoice.encrypted_fields_json))
        assert invoice.recurring_invoice_id == recurring.id
        assert invoice.recurring_scheduled_on == date(2026, 9, 25)
        assert invoice.invoice_number == f"RE-{invoice.id:06d}"
        assert values["status"] == "draft"
        assert values["invoice_date"] == "2026-09-25"
        assert values["due_date"] == "2026-10-02"
        assert values["payment_terms"] == "7_days"
        assert len(invoice.lines) == 1
        assert json.loads(cipher.decrypt(invoice.lines[0].encrypted_fields_json))["name"] == "Website-Betreuung"
        assert recurring.hub_next_run_on == date(2026, 10, 25)
        assert json.loads(cipher.decrypt(recurring.encrypted_fields_json))["next_invoice_date"] == "2026-10-25"
        pdf = db.scalar(select(HubFinanceGeneratedPdf).where(HubFinanceGeneratedPdf.document_id == invoice.id))
        assert pdf is not None and pdf.status == "queued" and pdf.is_zugferd
        assert pdf.template_name == "Standardrechnung"

        assert service.create_due(today=date(2026, 9, 25)).created_ids == ()
        recurring.hub_next_run_on = date(2026, 9, 25)
        db.commit()
        repeated = service.create_due(today=date(2026, 9, 25))
        assert repeated.created_ids == ()
        assert repeated.existing_count == 1
        assert len(db.scalars(select(HubFinanceInvoice)).all()) == 1
        assert recurring.hub_next_run_on == date(2026, 10, 25)


def test_missing_contact_leaves_series_due_without_creating_invoice():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        recurring = _recurring(db, cipher, contact=False)
        result = HubRecurringInvoiceGenerationService(db=db, cipher=cipher).create_due(today=date(2026, 9, 25))
        assert result.failed_ids == (recurring.id,)
        assert db.scalars(select(HubFinanceInvoice)).all() == []
        assert recurring.hub_next_run_on == date(2026, 9, 25)


def test_backfill_uses_imported_next_date_only_when_no_hub_cursor_exists():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        recurring = _recurring(db, cipher)
        service = HubRecurringInvoiceGenerationService(db=db, cipher=cipher)
        assert service.backfill_missing_cursors() == 0
        recurring.hub_next_run_on = None
        db.commit()
        assert service.backfill_missing_cursors() == 1
        assert recurring.hub_next_run_on == date(2026, 9, 25)
