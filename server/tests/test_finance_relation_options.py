import json
import os
from pathlib import Path

import pytest
from starlette.requests import Request

from app.api.routes import web
from app.main import create_app
from app.services.hub_finance_operations_shared import FINANCE_DOCUMENT_MODULES, finance_options, require_record
from app.services.hub_operations import HubOperationError, HubOperationService
from test_hub_finance_operations import env, values


@pytest.mark.parametrize("kind", ("offers", *FINANCE_DOCUMENT_MODULES))
def test_existing_filtered_customer_and_contacts_remain_editable(env, kind):
    result = env.service.execute(f"finance.{kind}.create", values(env, kind))
    env.customer.is_visible = False
    env.hidden.is_visible = False
    env.db.commit()
    record = require_record(env.service, kind, result.record_id)
    before = (record.customer_id, record.contact_id)

    assert not finance_options(env.service, kind)["customers"]
    options = finance_options(env.service, kind, record_id=record.id)
    assert [item.id for item in options["customers"]] == [env.customer.id]
    assert [item.id for item in options["contacts"]] == [env.contact.id]
    for category, selected in (("customers", env.customer.id), ("contacts", env.contact.id)):
        result_options = env.service.query("finance.options", {"kind": kind, "category": category, "record_id": str(record.id)})
        assert [item["id"] for item in result_options["items"]] == [str(selected)]

    denied = HubOperationService(db=env.db, cipher=env.cipher, actor=env.sales.username)
    with pytest.raises(HubOperationError):
        finance_options(denied, kind, record_id=record.id)
    with pytest.raises(HubOperationError):
        denied.query("finance.options", {"kind": kind, "category": "contacts", "record_id": str(record.id)})
    with pytest.raises(HubOperationError):
        finance_options(env.service, kind, record_id=999999)

    app = create_app()
    request = Request({"type": "http", "method": "GET", "path": f"/finance/{kind}/{record.id}", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("hub.test", 80), "session": {}, "app": app, "router": app.router})
    request.state.hub_user = env.user
    if kind == "offers":
        page = web.finance_offer_detail_page(record.id, request, env.db).body.decode()
    else:
        page = web.finance_document_detail_page(kind, record.id, request, env.db).body.decode()
    assert f'value="{env.customer.id}" data-finance-customer' in page
    assert f'data-customer-id="{env.customer.id}" selected>{env.contact.name if hasattr(env.contact, "name") else "Test contact"}' in page
    # A regular edit must keep the already-linked, filtered-out customer intact.
    env.service.execute(f"finance.{kind}.update", {"record_id": str(record.id), "customer_id": str(env.customer.id), "contact_id": str(env.contact.id)})
    assert (record.customer_id, record.contact_id) == before
    assert env.customer.is_visible is False

    directory = os.environ.get("HUB_FINANCE_RELATIONS_ARTIFACT_DIR")
    if directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{kind}.html").write_text(page, encoding="utf-8")
        (root / f"{kind}.json").write_text(json.dumps({"kind": kind, "id": record.id,
            "customer": env.customer.id, "customer_name": env.customer.name, "contact": env.contact.id}), encoding="utf-8")
