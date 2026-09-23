from app.core.templates import create_templates
from app.services.hub_finance_document_field_catalog import DUNNING_FIELDS, INVOICE_FIELDS, ORDER_FIELDS, RECURRING_INVOICE_FIELDS
from app.services.hub_finance_field_catalog import OFFER_FIELDS
from app.services.template_placeholders import EMAIL_TEMPLATE_CONTEXTS, contact_greeting, email_placeholders, pdf_placeholders
from app.services.zoho_account_field_catalog import ZOHO_ACCOUNT_FIELDS
from app.services.zoho_contact_field_catalog import ZOHO_CONTACT_FIELDS


def test_picker_scopes_document_fields_and_keeps_linked_sources():
    invoice = pdf_placeholders("invoices")
    offer = pdf_placeholders("offers")
    dunning = pdf_placeholders("dunnings")
    email = email_placeholders()

    assert "${Invoice.DueDate}" in {item.token for item in invoice}
    assert "${Invoice.DueDate}" not in {item.token for item in offer}
    assert "${Offer.ValidUntil}" in {item.token for item in offer}
    assert "${Dunning.SourceInvoiceNumber}" in {item.token for item in dunning}
    assert "${Contact.Name}" in {item.token for item in invoice}
    assert "${Customer.Website}" in {item.token for item in email}
    assert "${Lead.FirstName}" in {item.token for item in email}
    assert "${Case.CaseNumber}" in {item.token for item in email}
    assert "${Dunning.SourceInvoiceNumber}" in {item.token for item in email}
    assert "${Dunning.Status}" in {item.token for item in email}
    assert "${RecurringInvoice.NextInvoiceDate}" in {item.token for item in email}
    assert "${Task.DueAt}" in {item.token for item in email}
    assert "${Call.StartsAt}" in {item.token for item in email}
    assert "${Meeting.StartsAt}" in {item.token for item in email}
    assert "${Site.Domain}" in {item.token for item in email}
    assert "${Company.EmailSignature}" in {item.token for item in email}
    assert "${Company.EmailSignature}" not in {item.token for item in invoice}


def test_picker_renders_source_and_search_instead_of_flat_buttons():
    rendered = create_templates(directory="app/templates").get_template(
        "partials/template_placeholder_picker.html"
    ).render(
        placeholder_options=email_placeholders(),
        placeholder_picker_id="test-placeholder-results",
    )

    assert 'data-template-placeholder-source' in rendered
    assert 'data-template-placeholder-search' in rendered
    assert 'data-token="${Invoice.Number}"' in rendered
    assert 'data-token="${Company.EmailSignature}"' in rendered
    assert 'data-body-only' in rendered
    assert 'data-contexts="dunnings"' in rendered
    assert 'value="Verknüpfter Kontakt"' in rendered


def test_email_picker_places_context_before_source_and_search():
    rendered = create_templates(directory="app/templates").get_template(
        "partials/template_placeholder_picker.html"
    ).render(
        placeholder_options=email_placeholders(),
        placeholder_picker_id="test-placeholder-results",
        placeholder_context_options=EMAIL_TEMPLATE_CONTEXTS,
        placeholder_selected_context="invoices",
    )

    context_position = rendered.index("Kontextmodul")
    source_position = rendered.index("Quelle")
    search_position = rendered.index("Feld suchen")
    assert context_position < source_position < search_position
    assert 'name="context_module"' in rendered
    assert 'value="invoices" selected' in rendered
    assert "template-placeholder-controls is-contextual" in rendered


def test_email_placeholder_contexts_cover_all_hub_record_types():
    assert {item.key for item in EMAIL_TEMPLATE_CONTEXTS} == {
        "general", "customers", "leads", "cases", "offers", "orders", "invoices", "dunnings",
        "recurring-invoices", "tasks", "calls", "meetings", "sites",
    }
    by_token = {item.token: item for item in email_placeholders()}
    assert by_token["${Lead.FirstName}"].contexts == ("leads", "tasks", "calls", "meetings")
    assert by_token["${Invoice.Number}"].contexts == ("invoices", "dunnings")
    assert by_token["${Company.Name}"].contexts == ()


def test_email_picker_contains_every_usable_catalog_field():
    placeholders = email_placeholders()
    profile_keys_by_group = {
        group: {item.profile_key for item in placeholders if item.group == group and item.profile_key}
        for group in ("Kunde", "Verknüpfter Kontakt")
    }
    assert profile_keys_by_group["Kunde"] == {
        field.key for field in ZOHO_ACCOUNT_FIELDS if not field.sensitive and not field.subform_parent
    }
    assert profile_keys_by_group["Verknüpfter Kontakt"] == {field.key for field in ZOHO_CONTACT_FIELDS}

    tokens = {item.token for item in placeholders}
    assert "${Customer.WorkDomain}" in tokens
    assert "${Contact.SecondaryEmail}" in tokens
    assert "${Customer.DialfireApiToken}" not in tokens
    assert "${Customer.Iban}" not in tokens

    finance_fields_by_group = {
        "Aktuelles Angebot": OFFER_FIELDS,
        "Aktueller Auftrag": ORDER_FIELDS,
        "Aktuelle Rechnung": INVOICE_FIELDS,
        "Aktuelle Mahnung": DUNNING_FIELDS,
        "Aktuelle periodische Rechnung": RECURRING_INVOICE_FIELDS,
    }
    for group, fields in finance_fields_by_group.items():
        assert {item.profile_key for item in placeholders if item.group == group and item.profile_key} == {
            field.key for field in fields
        }
    assert {
        "${Offer.Reference}",
        "${Order.CancellationPeriod}",
        "${Invoice.RemainingAmount}",
        "${Dunning.Currency}",
        "${RecurringInvoice.CustomInterval}",
    } <= tokens
    assert "${Call.RecordingUrl}" in tokens
    assert "${Site.BridgeVersion}" in tokens


def test_contact_greeting_completes_partial_letter_salutation_and_has_neutral_fallback():
    assert contact_greeting(name="Anna Example", last_name="Example", letter_salutation="Sehr geehrte Frau") == "Sehr geehrte Frau Example"
    assert contact_greeting(name="Anna Example", last_name="Example", letter_salutation="Sehr geehrte Frau Example,") == "Sehr geehrte Frau Example"
    assert contact_greeting(name="Anna Example") == "Guten Tag Anna Example"
