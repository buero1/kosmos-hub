"""Every order field must be selectable and resolve from the same order."""

from dataclasses import replace
from html import escape
import json

import pytest
from lxml import html as html_parser

from app.core.templates import create_templates
from app.models.module_layout import ModuleLayout
from app.services.hub_finance import HubFinanceService
from app.services.hub_finance_document_field_catalog import ORDER_FIELDS
from app.services.hub_finance_documents import HubFinanceDocumentService, ORDER_MODULE
from app.services.hub_finance_pdf_generation import HubFinancePdfService
from app.services.hub_pdf_templates import HubPdfTemplateService
from app.services.template_placeholders import email_document_placeholders, pdf_document_placeholders, pdf_placeholders
from test_hub_finance_operations import env
from test_hub_finance_pdf_generation import _offer_values
from test_pdf_currency_display import snapshot_for


def order(env):
    offer = HubFinanceService(db=env.db, cipher=env.cipher).create_offer(
        customer_id=env.customer.id, contact_id=env.contact.id, submitted_values=_offer_values())
    fields = {
        'order_name': 'Website-Auftrag & Betreuung', 'status': 'confirmed', 'order_date': '2026-09-25',
        'outdoor_sales': 'Lukas Reich', 'contract_start': '2026-10-01',
        'contract_note': '<b>Individuell</b> & unveraendert', 'contract_term': '24',
        'payment_method': 'Lastschrift', 'payment_frequency': 'Monatlich',
        'cancellation_period': '1 Monat zum Monatsende', 'domain_request': 'beispiel.test',
        'order_intake_type': 'Schriftlich', 'contract_duration_years': '2,50',
        'cancellation_date': '2028-09-30', 'order_won_by': 'Stefanie Lier', 'agent_order_rating': '50%',
    }
    record = HubFinanceDocumentService(db=env.db, cipher=env.cipher).create_document(
        module=ORDER_MODULE, customer_id=env.customer.id, contact_id=env.contact.id, link_id=offer.id,
        submitted_values={**{f'document_field__{key}': value for key, value in fields.items()},
            'document_line__0__name': 'Betreuung', 'document_line__0__quantity': '1',
            'document_line__0__unit_price': '100', 'document_line__0__tax_rate': '19',
            'document_line__0__discount_percent': '0'})
    stored = json.loads(env.cipher.decrypt(record.encrypted_fields_json))
    stored.update(created_time='2026-09-23T08:30:00+00:00', modified_time='2026-09-25T10:45:00+00:00',
        internal_secret='must-never-be-a-placeholder')
    record.encrypted_fields_json = env.cipher.encrypt(json.dumps(stored))
    env.db.commit()
    expected = {**fields, 'order_number': record.order_number, 'customer': env.customer.name,
        'contact': 'Test contact', 'linked_offer': HubFinanceService.offer_number(offer),
        'status': 'Best\u00e4tigt', 'order_date': '25.09.2026', 'contract_start': '01.10.2026',
        'cancellation_date': '30.09.2028', 'contract_duration_years': '2,50',
        'created_time': '23.09.2026 10:30:00 CEST', 'modified_time': '25.09.2026 12:45:00 CEST'}
    return record, expected


def test_picker_has_all_order_catalog_fields_once_using_existing_email_tokens():
    options = pdf_placeholders('orders')
    by_key = {item.profile_key: item for item in options if item.group == 'Aktueller Auftrag' and item.profile_key}
    assert set(by_key) == {field.key for field in ORDER_FIELDS}
    assert len({item.token for item in options}) == len(options)
    email = {item.profile_key: item.token for item in email_document_placeholders('orders') if item.profile_key}
    assert {key: item.token for key, item in by_key.items()} == email
    rendered = create_templates(directory='app/templates').get_template('partials/template_placeholder_picker.html').render(
        placeholder_options=options, placeholder_picker_id='order-test')
    dom = html_parser.fromstring(rendered)
    for field in ORDER_FIELDS:
        item = by_key[field.key]
        assert item.label == field.label
        assert not item.contexts and not item.hint
        assert item.sample
        buttons = dom.xpath('//button[@data-source="Aktueller Auftrag" and @data-token=$token]', token=item.token)
        assert len(buttons) == 1 and field.label in buttons[0].text_content()
        assert HubPdfTemplateService._preview_html(f'<p>{item.token}</p>') == f'<p>{escape(item.sample)}</p>'
    assert {'${Order.Number}', '${Order.Title}', '${Order.Date}', '${Order.GrossTotal}', '${Order.PaymentTerms}'} <= {item.token for item in options}
    for kind in ['offers', 'invoices', 'dunnings']:
        assert not any(item.token.startswith('${Order.') for item in pdf_placeholders(kind))


@pytest.mark.parametrize('custom_layout', [False, True])
def test_all_order_fields_resolve_in_pdf_blocks_and_legal_terms(env, custom_layout):
    record, expected = order(env)
    if custom_layout:
        keys = [field.key for field in reversed(ORDER_FIELDS)]
        env.db.add(ModuleLayout(layout_key=ORDER_MODULE.layout_key, item_order_json=json.dumps(
            keys[:2] + ['__show_more__'] + keys[2:])))
        env.db.commit()
    service = HubFinancePdfService(db=env.db, cipher=env.cipher)
    snapshot = service._snapshot(document_type='orders', document_id=record.id)
    assert snapshot.document_fields == expected
    definitions = [item for item in pdf_document_placeholders('orders') if item.profile_key]
    markup = ''.join(f'<p id="field-{item.profile_key}">{item.token}</p>' for item in definitions)
    content = HubPdfTemplateService._default_content('orders')
    content['blocks']['intro']['content_html'] = markup
    rendered = service._render_html(snapshot=snapshot, content=content, legal_terms_html=markup)
    dom = html_parser.fromstring(rendered)
    for key, value in expected.items():
        elements = dom.xpath('//p[@id=$id]', id=f'field-{key}')
        assert len(elements) == 2
        assert all(element.text_content() == value for element in elements)
    assert '&lt;b&gt;Individuell&lt;/b&gt; &amp; unveraendert' in rendered
    assert 'must-never-be-a-placeholder' not in rendered
    assert '${' not in rendered


def test_missing_optional_order_values_are_blank_not_preview_samples(env):
    record, _ = order(env)
    stored = json.loads(env.cipher.decrypt(record.encrypted_fields_json))
    keys = ['contract_start', 'contract_note', 'outdoor_sales', 'domain_request', 'contract_duration_years',
        'cancellation_date', 'order_won_by', 'agent_order_rating']
    for key in keys:
        stored.pop(key)
    record.encrypted_fields_json = env.cipher.encrypt(json.dumps(stored))
    record.offer = None
    env.db.commit()
    service = HubFinancePdfService(db=env.db, cipher=env.cipher)
    snapshot = service._snapshot(document_type='orders', document_id=record.id)
    keys.append('linked_offer')
    tokens = {item.profile_key: item.token for item in pdf_document_placeholders('orders') if item.profile_key}
    content = HubPdfTemplateService._default_content('orders')
    content['blocks']['intro']['content_html'] = '<p>' + '|'.join(tokens[key] for key in keys) + '</p>'
    dom = html_parser.fromstring(service._render_html(snapshot=snapshot, content=content))
    assert dom.xpath('//div[@class="intro"]')[0].text_content() == '|' * (len(keys) - 1)


def test_record_text_is_not_interpreted_as_markup_or_nested_placeholders():
    snapshot = replace(snapshot_for('orders'), document_fields={
        'contract_note': '<script>bad()</script> ${Order.DomainRequest}',
        'domain_request': 'must-not-replace-nested-text',
    })
    markup = '<p>${Order.ContractNote}</p>'
    content = HubPdfTemplateService._default_content('orders')
    content['blocks']['intro']['content_html'] = markup
    rendered = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot, content=content, legal_terms_html=markup)
    assert rendered.count(escape(snapshot.document_fields['contract_note'])) == 2
    assert '<script>' not in rendered and 'must-not-replace-nested-text' not in rendered


@pytest.mark.parametrize('kind', ['offers', 'invoices', 'dunnings'])
def test_order_fields_do_not_leak_into_other_document_types(kind):
    snapshot = replace(snapshot_for(kind), document_fields={'contract_note': 'Wrong order'})
    content = HubPdfTemplateService._default_content(kind)
    content['blocks']['intro']['content_html'] = '<p>${Order.ContractNote}</p>'
    rendered = HubFinancePdfService(db=None, cipher=None)._render_html(snapshot=snapshot, content=content)
    assert 'Wrong order' not in rendered
    assert '${Order.ContractNote}' not in rendered
