import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.models.hub_finance_documents import HubFinanceInvoice
from app.models.hub_invoice_email_batch import HubInvoiceEmailBatch, HubInvoiceEmailBatchItem
from app.services.hub_finance_documents import HubFinanceDocumentService, INVOICE_MODULE
from app.services.hub_finance_operations_shared import finance_invoice_page
from app.services.hub_invoice_email_batches import HubInvoiceEmailBatchService
from app.services.hub_invoice_email_delivery import InvoiceEmailDelivery, invoice_email_deliveries
from app.services.hub_operations import HubOperationService
from test_hub_finance_operations import env, values


def invoice(env):
    result = env.service.execute("finance.invoices.create", values(env, "invoices"))
    return env.db.get(HubFinanceInvoice, result.record_id)


def attempt(env, record, status, sent_at=None):
    batch = HubInvoiceEmailBatch(
        review_nonce=uuid4().hex, actor="admin", sender_email="sender@example.test",
        template_id="test", status="completed",
    )
    env.db.add(batch)
    env.db.flush()
    item = HubInvoiceEmailBatchItem(
        batch_id=batch.id, invoice_id=record.id, status=status, sent_at=sent_at,
        encrypted_payload_json=env.cipher.encrypt(json.dumps({"invoice_id": record.id})),
        updated_at=datetime(2030, 1, 1),
    )
    env.db.add(item)
    env.db.flush()
    return batch, item


@pytest.mark.parametrize("status,label", [
    (None, "Noch nicht versendet"), ("queued", "Wartet"),
    ("sending", "Wird versendet"), ("sent", "Versendet"),
    ("failed", "Fehlgeschlagen"), ("uncertain", "Status unklar"),
    ("unexpected", "Status unklar"),
])
def test_delivery_labels_and_no_false_success_time(env, status, label):
    record = invoice(env)
    if status:
        attempt(env, record, status)
    delivery = invoice_email_deliveries(env.db, [record.id])[record.id]
    assert delivery.label == label
    assert delivery.sent_at is None and delivery.sent_at_display == "-"


@pytest.mark.parametrize("timestamp,expected", [
    (datetime(2026, 9, 25, 9, 34), "25.09.2026 11:34"),
    (datetime(2026, 12, 25, 23, 34), "26.12.2026 00:34"),
])
def test_success_time_is_utc_and_not_the_journal_update_time(env, timestamp, expected):
    record = invoice(env)
    snapshot = record.encrypted_fields_json
    batch, _ = attempt(env, record, "sent", timestamp)
    delivery = invoice_email_deliveries(env.db, [record.id])[record.id]
    assert delivery.sent_at_display == expected
    status = HubInvoiceEmailBatchService(db=env.db, cipher=env.cipher).status(batch_id=batch.id, actor="admin")
    assert status["items"][0]["delivery_status"] == "Versendet"
    assert status["items"][0]["delivery_sent_at"] == expected
    assert record.encrypted_fields_json == snapshot
    with pytest.raises(ValueError, match="nicht gefunden"):
        HubInvoiceEmailBatchService(db=env.db, cipher=env.cipher).status(batch_id=batch.id, actor="other")


@pytest.mark.parametrize("last_status", ["queued", "sending", "failed", "uncertain"])
def test_resend_does_not_erase_previous_success(env, last_status):
    record = invoice(env)
    attempt(env, record, "sent", datetime(2026, 9, 24, 7))
    attempt(env, record, "sent", datetime(2026, 9, 25, 8))
    # Even a corrupt failure timestamp must not count as successful delivery.
    batch, _ = attempt(env, record, last_status, datetime(2026, 9, 26, 9))
    delivery = invoice_email_deliveries(env.db, [record.id])[record.id]
    assert delivery.status == last_status
    assert delivery.sent_at_display == "25.09.2026 10:00"
    response = HubInvoiceEmailBatchService(db=env.db, cipher=env.cipher).status(batch_id=batch.id, actor="admin")
    assert response["items"][0]["delivery_sent_at"] == delivery.sent_at_display


def test_one_bounded_query_no_payload_decryption_and_no_cross_invoice_leak(env):
    first, second = invoice(env), invoice(env)
    attempt(env, first, "sent", datetime(2026, 9, 25, 9))
    _batch, item = attempt(env, second, "failed")
    item.encrypted_payload_json = "unreadable and irrelevant to the summary"
    env.db.flush()
    ids = [first.id, second.id]
    statements = []
    def record_query(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(env.db.bind, "before_cursor_execute", record_query)
    try:
        assert invoice_email_deliveries(env.db, []) == {}
        assert not statements
        result = invoice_email_deliveries(env.db, ids)
    finally:
        event.remove(env.db.bind, "before_cursor_execute", record_query)
    assert len(statements) == 1 and set(result) == set(ids)
    assert result[first.id].status == "sent" and result[second.id].status == "failed"
    assert invoice_email_deliveries(env.db, [second.id]) == {second.id: result[second.id]}


def test_list_detail_and_customer_scope(env):
    record = invoice(env)
    attempt(env, record, "sent", datetime(2026, 9, 25, 9))
    service = HubFinanceDocumentService(db=env.db, cipher=env.cipher)
    expected = InvoiceEmailDelivery("sent", datetime(2026, 9, 25, 9))
    assert service.list_invoice_page(page=1).entries[0].email_delivery == expected
    assert service.list_documents(module=INVOICE_MODULE)[0].email_delivery == expected
    assert service.list_customer_documents(module=INVOICE_MODULE, customer_id=record.customer_id)[0].email_delivery == expected
    assert service.get_detail(module=INVOICE_MODULE, document_id=record.id).email_delivery == expected
    limited = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    assert finance_invoice_page(limited, 1).entries == ()
    env.access.assign_record(module_key="customers", record_id=env.customer.id, owner_user_id=env.sales.id, team_id=None)
    env.db.flush()
    assert finance_invoice_page(limited, 1).entries[0].email_delivery == expected


def test_rendered_list_detail_and_browser_fixtures(env):
    record = invoice(env)
    app = create_app()
    def request(path):
        req = Request({"type": "http", "method": "GET", "path": path, "query_string": b"",
                       "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
        req.state.hub_user = env.user
        return req
    before = web.finance_documents_page("invoices", request("/finance/invoices"), env.db).body.decode()
    assert "Noch nicht versendet" in before
    batch, _item = attempt(env, record, "sent", datetime(2026, 9, 25, 9, 34))
    after = web.finance_documents_page("invoices", request("/finance/invoices"), env.db).body.decode()
    detail = web.finance_document_detail_page("invoices", record.id, request(f"/finance/invoices/{record.id}"), env.db).body.decode()
    for html in (after, detail):
        assert "Versandstatus" in html and "Versendet am" in html
        assert "25.09.2026 11:34" in html and ">Versendet<" in html
        assert 'name="document_field__email_delivery"' not in html
    directory = os.environ.get("HUB_INVOICE_DELIVERY_ARTIFACT_DIR")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        for name, html in (("before", before), ("after", after), ("detail", detail)):
            (root / f"{name}.html").write_text(html, encoding="utf-8")
        (root / "data.json").write_text(json.dumps({"invoice": record.id, "batch": batch.id,
            "result": HubInvoiceEmailBatchService(db=env.db, cipher=env.cipher).status(batch_id=batch.id, actor="admin")}), encoding="utf-8")


def test_additive_timestamp_migration_is_idempotent(env):
    import ast
    import inspect as python_inspect
    import app.main as main
    from sqlalchemy import inspect, text
    record = invoice(env)
    _batch, item = attempt(env, record, "sent")
    item_id = item.id
    env.db.commit()
    with env.db.bind.begin() as connection:
        connection.execute(text("ALTER TABLE hub_invoice_email_batch_items DROP COLUMN sent_at"))
    # Run the actual additive block apart from unrelated MySQL-only migrations.
    tree = ast.parse(python_inspect.getsource(main._ensure_phase_one_schema))
    block = next(node for node in tree.body[0].body if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "'hub_invoice_email_batch_items' in table_names")
    code = compile(ast.Module(body=[block], type_ignores=[]), "invoice timestamp migration", "exec")
    for _ in range(2):
        exec(code, {"engine": env.db.bind, "inspector": inspect(env.db.bind),
                    "table_names": inspect(env.db.bind).get_table_names(), "text": text, "logger": main.logger})
    assert "sent_at" in {column["name"] for column in inspect(env.db.bind).get_columns("hub_invoice_email_batch_items")}
    env.db.expire_all()
    preserved = env.db.get(HubInvoiceEmailBatchItem, item_id)
    assert preserved.status == "sent" and preserved.sent_at is None
