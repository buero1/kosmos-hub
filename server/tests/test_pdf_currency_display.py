from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.hub_finance_pdf_generation import FinancePdfSnapshot, HubFinancePdfService
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.template_placeholders import DOCUMENT_NAMES


def snapshot_for(kind, currency="EUR"):
    line = SimpleNamespace(name="Website", description="Testposition", sku="WEB", quantity="2", unit="Einmalig",
        unit_price="100", discount_percent="10", tax_rate="19", amount_net=Decimal("180"), tax_amount=Decimal("34.20"))
    return FinancePdfSnapshot(document_type=kind, identifier="TEST-1", document_title="Waehrungspruefung", document_status="draft",
        document_date="2026-09-20", due_date="2026-10-04", source_invoice_number="TEST-0", valid_until="2026-10-20", payment_terms="",
        currency=currency, customer_name="PDF-Testfirma", contact_name="", billing_street="Testweg 1", billing_postal_code="12345",
        billing_city="Teststadt", billing_country_code="DE", lines=(line,),
        totals=SimpleNamespace(subtotal_net=Decimal("200"), discount_total=Decimal("20"), tax_total=Decimal("34.20"), total_gross=Decimal("214.20")))


@pytest.mark.parametrize("amount, expected", [("1234.565", "1.234,57"), ("0", "0,00"), ("-12.5", "-12,50")])
@pytest.mark.parametrize("currency, suffix", [("EUR", "\u20ac"), ("CHF", "CHF"), ("USD", "USD")])
def test_money_uses_euro_symbol_without_changing_other_currencies_or_rounding(amount, expected, currency, suffix):
    assert HubFinancePdfService._money(Decimal(amount), currency) == f"{expected} {suffix}"


@pytest.mark.parametrize("kind", ["offers", "orders", "invoices", "dunnings"])
def test_all_pdf_amounts_and_placeholders_use_euro_symbol(kind):
    snapshot = snapshot_for(kind)
    content = HubPdfTemplateService._default_content(kind)
    content["positions"]["show_totals"] = True
    namespace = DOCUMENT_NAMES[kind][0]
    content["blocks"]["intro"]["content_html"] = (
        "<p>${netTotal} / ${taxTotal} / ${grossTotal}</p>"
        + f"<p>${{{namespace}.NetTotal}} / ${{{namespace}.TaxTotal}} / ${{{namespace}.GrossTotal}}</p>")
    html = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot, content=content,
        legal_terms_html="<p>${grossTotal}</p>")
    for amount in ("100,00", "180,00", "200,00", "20,00", "34,20", "214,20"):
        assert amount + " \u20ac" in html
    assert "EUR" not in html
    assert "${" not in html
    assert snapshot.currency == "EUR"


def test_invoice_machine_readable_data_keeps_currency_code():
    data = HubFinancePdfService._zugferd_data(snapshot_for("invoices"))
    assert data["BT-5"] == "EUR"
    assert data["BT-110-1"] == "EUR"
