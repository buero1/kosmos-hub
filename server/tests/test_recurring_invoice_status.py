import json
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.hub_finance_documents import HubFinanceInvoice, HubFinanceRecurringInvoice
from app.services.hub_finance_documents import HubFinanceDocumentError, HubFinanceDocumentService, RECURRING_INVOICE_MODULE
from app.services.hub_recurring_invoice_generation import HubRecurringInvoiceGenerationService
from app.services.zoho_books_recurring_invoice_import import ZohoBooksRecurringInvoiceImportService
from test_hub_finance_operations import env, values
from test_zoho_books_recurring_invoice_import import FakeBooksRecurringInvoiceReader


def create(env, **fields):
    result = env.service.execute('finance.recurring-invoices.create', {
        **values(env, 'recurring-invoices'),
        'document_field__start_date': '2026-01-25',
        'document_field__next_invoice_date': '2026-09-25',
        **{f'document_field__{key}': value for key, value in fields.items()},
    })
    return env.db.get(HubFinanceRecurringInvoice, result.record_id)


def raw(env, record):
    return json.loads(env.cipher.decrypt(record.encrypted_fields_json))


def update(env, record, **fields):
    return env.service.execute('finance.recurring-invoices.update', {
        'record_id': str(record.id),
        **{f'document_field__{key}': value for key, value in fields.items()},
    })


def assert_stopped(env, record):
    assert record.hub_next_run_on is None
    assert raw(env, record)['next_invoice_date'] == ''
    detail = HubFinanceDocumentService(db=env.db, cipher=env.cipher).get_detail(
        module=RECURRING_INVOICE_MODULE, document_id=record.id)
    field = next(field for field in detail.fields if field.key == 'next_invoice_date')
    assert field.value == field.form_value == ''
    assert field.required is False


@pytest.mark.parametrize('status', ['paused', 'ended'])
@pytest.mark.parametrize('next_date', ['', '2026-09-25', 'invalid-stale-value'])
def test_create_stopped_series_ignores_next_date(env, status, next_date):
    record = create(env, status=status, next_invoice_date=next_date)
    assert_stopped(env, record)
    service = HubRecurringInvoiceGenerationService(db=env.db, cipher=env.cipher)
    assert service.backfill_missing_cursors() == 0
    assert service.create_due(today=date(2030, 1, 1)).created_ids == ()
    assert env.db.scalars(select(HubFinanceInvoice)).all() == []


@pytest.mark.parametrize('status', ['paused', 'ended'])
def test_partial_status_update_clears_schedule_and_reactivation_needs_date(env, status):
    record = create(env)
    original_lines = [raw(env, line) for line in record.lines]
    update(env, record, status=status)
    assert_stopped(env, record)
    assert [raw(env, line) for line in record.lines] == original_lines
    assert (record.customer_id, record.contact_id) == (env.customer.id, env.contact.id)
    update(env, record, name='Still stopped')
    assert_stopped(env, record)
    with pytest.raises(HubFinanceDocumentError, match='Rechnungsdatum.*erforderlich'):
        update(env, record, status='active')
    assert_stopped(env, record)
    assert raw(env, record)['status'] == status
    update(env, record, status='active', next_invoice_date='2027-01-25')
    assert record.hub_next_run_on == date(2027, 1, 25)
    assert raw(env, record)['next_invoice_date'] == '2027-01-25'
    service = HubRecurringInvoiceGenerationService(db=env.db, cipher=env.cipher)
    assert service.create_due(today=date(2027, 1, 24)).created_ids == ()
    assert len(service.create_due(today=date(2027, 1, 25)).created_ids) == 1


def test_active_series_still_requires_next_date(env):
    with pytest.raises(HubFinanceDocumentError, match='Rechnungsdatum.*erforderlich'):
        create(env, next_invoice_date='')


@pytest.mark.parametrize('status', ['paused', 'ended'])
@pytest.mark.parametrize('cursor_present', [True, False])
def test_legacy_stopped_dates_are_hidden_and_cleaned_not_backfilled(env, status, cursor_present):
    record = create(env)
    active = create(env)
    active_encrypted = active.encrypted_fields_json
    record.encrypted_fields_json = env.cipher.encrypt(json.dumps({**raw(env, record), 'status': status}))
    if not cursor_present:
        record.hub_next_run_on = None
    env.db.commit()
    detail = env.service.query('finance.read', {'kind': 'recurring-invoices', 'record_id': str(record.id)})
    assert detail['input_values']['document_field__next_invoice_date'] == ''
    service = HubRecurringInvoiceGenerationService(db=env.db, cipher=env.cipher)
    assert service.backfill_missing_cursors() == 1
    assert_stopped(env, record)
    assert service.backfill_missing_cursors() == 0
    assert active.encrypted_fields_json == active_encrypted
    assert active.hub_next_run_on == date(2026, 9, 25)


@pytest.mark.parametrize('status', ['paused', 'ended'])
def test_generator_does_not_create_invoices_for_stopped_legacy_series(env, status):
    record = create(env)
    record.encrypted_fields_json = env.cipher.encrypt(json.dumps({**raw(env, record), 'status': status}))
    env.db.commit()
    service = HubRecurringInvoiceGenerationService(db=env.db, cipher=env.cipher)
    result = service.create_due(today=date(2030, 1, 1))
    assert result.created_ids == result.failed_ids == ()
    assert env.db.scalars(select(HubFinanceInvoice)).all() == []


@pytest.mark.parametrize(('end_date', 'expected_count'), [('2026-09-24', 0), ('2026-09-25', 1)])
def test_automatic_end_also_clears_both_dates(env, end_date, expected_count):
    record = create(env, end_date=end_date)
    service = HubRecurringInvoiceGenerationService(db=env.db, cipher=env.cipher)
    result = service.create_due(today=date(2026, 9, 25))
    assert len(result.created_ids) == expected_count
    assert result.failed_ids == ()
    assert raw(env, record)['status'] == 'ended'
    assert_stopped(env, record)
    assert service.backfill_missing_cursors() == 0
    assert service.create_due(today=date(2030, 1, 1)).created_ids == ()
    assert len(env.db.scalars(select(HubFinanceInvoice)).all()) == expected_count


@pytest.mark.parametrize('status', ['stopped', 'expired'])
def test_import_of_stopped_series_clears_cursor_including_existing_record(env, status):
    reader = FakeBooksRecurringInvoiceReader()
    env.customer.zoho_id = 'crm-customer-1'
    env.contact.zoho_id = 'crm-contact-1'
    env.db.commit()
    service = ZohoBooksRecurringInvoiceImportService(db=env.db, cipher=env.cipher, books_service=reader)
    for imported_status in ['active', status, status]:
        reader.invoice['status'] = imported_status
        service.start_all(requested_by='admin')
        env.db.commit()
        assert service.process_next_invoice() == 'succeeded'
        assert service.process_next_invoice() == 'completed'
        record = env.db.scalar(select(HubFinanceRecurringInvoice).where(HubFinanceRecurringInvoice.zoho_books_id == '7001'))
        if imported_status == 'active':
            assert record.hub_next_run_on is not None
        else:
            assert_stopped(env, record)


def test_render_recurring_status_forms(env):
    app = create_app()
    root = os.environ.get('HUB_RECURRING_STATUS_ARTIFACT_DIR')
    for status in ['active', 'paused', 'ended']:
        record = create(env, status=status)
        request = Request({'type': 'http', 'method': 'GET', 'path': f'/finance/recurring-invoices/{record.id}',
            'query_string': b'', 'headers': [], 'scheme': 'http', 'server': ('hub.test', 80), 'session': {},
            'app': app, 'router': app.router})
        request.state.hub_user = env.user
        page = web.finance_document_detail_page('recurring-invoices', record.id, request, env.db).body.decode()
        assert 'function syncNextDate()' in page
        expected = '2026-09-25' if status == 'active' else ''
        assert f'name="document_field__next_invoice_date" value="{expected}"' in page
        if root:
            directory = Path(root)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f'{status}.html').write_text(page, encoding='utf-8')
