"""Invoice bank placeholders are explicit customer data, never company defaults."""

from dataclasses import replace
import json

import pytest
from lxml import html as html_parser
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import SecretCipher
from app.core.templates import create_templates
from app.db.base import Base
from app.models.customer import Customer
from app.models.customer_contact import CustomerContact
from app.services.customer_directory import CustomerDirectoryService
from app.services.hub_finance_documents import INVOICE_MODULE, HubFinanceDocumentService
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.template_placeholders import email_placeholders, pdf_placeholders
from test_hub_finance_pdf_generation import _invoice_values
from test_pdf_currency_display import snapshot_for


TOKENS = {
    "${Customer.Iban}": ("IBAN", "iban"),
    "${Customer.Bic}": ("BIC", "bic"),
    "${Customer.Bank}": ("Bankname", "bank"),
    "${Customer.CustomerNumber}": ("Kundennummer", "customer_number"),
}


def create_invoice(db, cipher, customer):
    contact = CustomerContact(customer=customer,
        encrypted_profile_json=cipher.encrypt(json.dumps({"fields": {"Name": "Test Kontakt"}})))
    db.add(contact)
    db.flush()
    return HubFinanceDocumentService(db=db, cipher=cipher).create_document(
        module=INVOICE_MODULE, customer_id=customer.id, contact_id=contact.id, link_id=None,
        submitted_values=_invoice_values(),
    )


def test_invoice_picker_exposes_all_four_customer_fields_once_and_has_samples():
    options = pdf_placeholders("invoices")
    dom = html_parser.fromstring(create_templates(directory="app/templates").get_template(
        "partials/template_placeholder_picker.html"
    ).render(placeholder_options=options, placeholder_picker_id="invoice-customer-test"))
    for token, (label, key) in TOKENS.items():
        matches = [item for item in options if item.token == token]
        assert len(matches) == 1
        item = matches[0]
        assert (item.label, item.profile_key, item.group) == (label, key, "Kunde")
        assert item.sample
        buttons = dom.xpath('//button[@data-source="Kunde" and @data-token=$token]', token=token)
        assert len(buttons) == 1 and label in buttons[0].text_content()
        assert HubPdfTemplateService._preview_html(f"<p>{token}</p>") == f"<p>{item.sample}</p>"
    for kind in ("offers", "orders", "dunnings"):
        assert "${Customer.Iban}" not in {item.token for item in pdf_placeholders(kind)}
    assert "${Customer.Iban}" not in {item.token for item in email_placeholders()}


@pytest.mark.parametrize("storage", ["labels", "keys", "renamed"])
def test_invoice_uses_linked_customer_bank_data_and_preserves_protected_crm_reads(storage):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    labels = {"iban": "IBAN", "bic": "BIC", "bank": "Bank", "customer_number": "Kunde-Nummer"}
    expected = {"iban": "DE89 3704 0044 0532 0130 00", "bic": "TESTDEFFXXX",
                "bank": "Kundenbank <Test> & Partner", "customer_number": "001042"}
    stored = {key if storage == "keys" else "Old " + key if storage == "renamed" else labels[key]: value
              for key, value in expected.items()}
    stored["Dialfire-API-Token"] = "private-api-secret"
    profile = {"fields": stored, "field_metadata": {
        key: {"label": "Old " + key if storage == "renamed" else label} for key, label in labels.items()
    }}
    with Session(engine) as db:
        customer = Customer(name="PDF Test", encrypted_profile_json=cipher.encrypt(json.dumps(profile)))
        unrelated = Customer(name="Other customer", encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": {"IBAN": "WRONG-CUSTOMER-IBAN", "Bank": "Wrong bank"},
        })))
        db.add_all([customer, unrelated])
        db.flush()
        document = create_invoice(db, cipher, customer)
        service = HubFinancePdfService(db=db, cipher=cipher)
        snapshot = service._snapshot(document_type="invoices", document_id=document.id)
        assert {key: snapshot.customer_fields[key] for key in expected} == expected
        assert "dialfire_api_token" not in snapshot.customer_fields
        safe_detail = CustomerDirectoryService(db=db, cipher=cipher).get_detail(customer_id=customer.id)
        assert "iban" not in {field.key for field in safe_detail.profile_fields}
        content = HubPdfTemplateService._default_content("invoices")
        content["blocks"]["payment"]["content_html"] = "<p>" + " / ".join(TOKENS) + "</p>"
        rendered = service._render_html(snapshot=snapshot, content=content)
        text = html_parser.fromstring(rendered).xpath('//div[@class="payment"]')[0].text_content()
        assert text == " / ".join(expected[key] for _, key in TOKENS.values())
        assert "Kundenbank &lt;Test&gt; &amp; Partner" in rendered
        assert "${" not in rendered and "Geschützt" not in rendered
        assert "WRONG-CUSTOMER-IBAN" not in rendered and "private-api-secret" not in rendered
    engine.dispose()


@pytest.mark.parametrize("bank_value", [None, "", {}, [], False])
def test_missing_or_invalid_customer_bank_details_remain_blank(bank_value):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = SecretCipher("a" * 32)
    with Session(engine) as db:
        customer = Customer(name="No bank", encrypted_profile_json=cipher.encrypt(json.dumps({
            "fields": {"IBAN": bank_value, "BIC": bank_value, "Bank": bank_value},
        })))
        db.add(customer)
        db.flush()
        document = create_invoice(db, cipher, customer)
        service = HubFinancePdfService(db=db, cipher=cipher)
        snapshot = service._snapshot(document_type="invoices", document_id=document.id)
        content = HubPdfTemplateService._default_content("invoices")
        content["blocks"]["payment"]["content_html"] = "<p>" + "|".join(TOKENS) + "</p>"
        dom = html_parser.fromstring(service._render_html(snapshot=snapshot, content=content))
        assert dom.xpath('//div[@class="payment"]')[0].text_content() == "|||"
    engine.dispose()


@pytest.mark.parametrize("kind", ["offers", "orders", "dunnings"])
def test_customer_bank_tokens_are_not_resolved_outside_invoices(kind):
    snapshot = replace(snapshot_for(kind), customer_fields={"iban": "not-authorized-here", "bank": "no-bank"})
    content = HubPdfTemplateService._default_content(kind)
    content["blocks"]["payment"]["content_html"] = "<p>${Customer.Iban}|${Customer.Bic}|${Customer.Bank}</p>"
    dom = html_parser.fromstring(HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot, content=content))
    assert dom.xpath('//div[@class="payment"]')[0].text_content() == "||"
