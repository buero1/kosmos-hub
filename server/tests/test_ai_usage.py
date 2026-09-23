from datetime import UTC, datetime
from decimal import Decimal
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.ai_usage import AiUsageRequest
from app.models.hub_user import HubUser
from app.services.ai_usage import AiUsageTrace, estimate_cost, request_openai_json, usage_report
from app.services.hub_operations import HubOperationError, HubOperationService
from app.core.security import SecretCipher


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([HubUser(username="admin", password_hash="hash", role="admin"),
                         HubUser(username="alice", password_hash="hash", role="operator"),
                         HubUser(username="bob", password_hash="hash", role="operator")])
        session.commit()
        yield session
    engine.dispose()


def payload():
    return {"model": "gpt-5.6-sol", "instructions": "PRIVATE INSTRUCTION", "input": "PRIVATE DOCUMENT",
            "tools": [{"type": "function", "name": "propose_hub_actions"}]}


def response(**changes):
    return {"model": "gpt-5.6-sol", "status": "completed", "output": [],
        "usage": {"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 8000},
                  "output_tokens": 1000, "output_tokens_details": {"reasoning_tokens": 600}}, **changes}


def test_pricing_uses_cached_input_and_does_not_double_bill_reasoning():
    assert estimate_cost("gpt-5.6-sol", 10000, 8000, 1000) == (Decimal("0.0312"), Decimal("0.0332"))
    assert estimate_cost("gpt-5.6-sol", 300000, 0, 1000) == (Decimal("2.43"), Decimal("3.03"))
    assert estimate_cost("future-model", 100, 0, 100) == (None, None)
    assert estimate_cost("gpt-5.6-sol", 100, None, 100) == (None, None)
    assert estimate_cost("gpt-5.6-sol", 100, 101, 100) == (None, None)


def test_reported_cache_writes_are_priced_once_and_unknown_writes_keep_range(db):
    assert estimate_cost("gpt-5.6-sol", 10000, 8000, 1000, 1000) == (Decimal("0.0322"), Decimal("0.0322"))
    assert estimate_cost("gpt-5.6-sol", 300000, 0, 1000, 10000) == (Decimal("2.45"), Decimal("2.45"))
    assert estimate_cost("gpt-5.6-sol", 100, 90, 10, 11) == (None, None)
    trace = AiUsageTrace(db=db, actor="alice", feature="hub-agent")
    row_id = trace.start(payload())
    data = response()
    data["usage"]["input_tokens_details"]["cache_write_tokens"] = 1000
    trace.finish(row_id, payload=payload(), response=data, http_status=200)
    row = db.get(AiUsageRequest, row_id)
    assert row.sizes["cache_write_tokens"] == 1000
    assert row.estimated_usd == row.estimated_usd_upper == Decimal("0.0322")


def test_meter_survives_business_rollback_and_has_no_content_or_secrets(db, monkeypatch):
    class Response:
        status = 200
        headers = {"x-request-id": "req-safe"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(response()).encode()
    monkeypatch.setattr("app.services.ai_usage.request.urlopen", lambda *args, **kwargs: Response())
    trace = AiUsageTrace(db=db, actor="alice", feature="hub-agent", conversation_id=7)
    for _ in range(2):
        request_openai_json(api_key="sk-private-key", payload=payload(), timeout=1, trace=trace)
    db.rollback()
    rows = db.scalars(select(AiUsageRequest).order_by(AiUsageRequest.sequence)).all()
    assert len(rows) == 2 and [row.sequence for row in rows] == [1, 2]
    assert len({row.run_id for row in rows}) == 1
    assert rows[0].provider_request_id == "req-safe"
    assert rows[0].reasoning_tokens == 600
    assert rows[0].estimated_usd == Decimal("0.0312")
    assert "PRIVATE" not in str([row.__dict__ for row in rows])
    assert "sk-private" not in str([row.__dict__ for row in rows])
    assert trace.estimated_upper == Decimal("0.0664")


@pytest.mark.parametrize("failure", ["quota", "network", "invalid", "incomplete"])
def test_failed_or_incomplete_calls_are_not_invisible_or_mislabeled_free(db, monkeypatch, failure):
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return b"invalid" if failure == "invalid" else json.dumps(response(status="incomplete")).encode()
    def call(*args, **kwargs):
        if failure == "quota":
            raise HTTPError("https://api.openai.com", 429, "quota", {}, io.BytesIO(
                b'{"error":{"code":"credit_balance_exhausted","message":"PRIVATE KEY"}}'))
        if failure == "network":
            raise URLError("PRIVATE URL")
        return Response()
    monkeypatch.setattr("app.services.ai_usage.request.urlopen", call)
    trace = AiUsageTrace(db=db, actor="alice", feature="hub-agent")
    if failure == "incomplete":
        request_openai_json(api_key="test", payload=payload(), timeout=1, trace=trace)
    else:
        with pytest.raises((HTTPError, URLError, ValueError)):
            request_openai_json(api_key="test", payload=payload(), timeout=1, trace=trace)
    row = db.scalars(select(AiUsageRequest)).one()
    assert row.status == ("incomplete" if failure == "incomplete" else "error")
    assert (row.estimated_usd is not None) == (failure == "incomplete")
    assert "PRIVATE" not in str(row.__dict__)
    if failure == "quota":
        assert row.http_status == 429 and row.error_code == "credit_balance_exhausted"


def test_no_paid_request_if_meter_cannot_start(db, monkeypatch):
    trace = AiUsageTrace(db=db, actor="alice", feature="hub-agent")
    def fail(*args): raise RuntimeError("meter unavailable")
    monkeypatch.setattr(trace, "start", fail)
    monkeypatch.setattr("app.services.ai_usage.request.urlopen", lambda *a, **k: pytest.fail("Must not spend unmetered"))
    with pytest.raises(RuntimeError):
        request_openai_json(api_key="test", payload=payload(), timeout=1, trace=trace)


def test_usage_missing_is_unknown_and_reports_are_permission_scoped(db):
    for actor in ("alice", "bob"):
        trace = AiUsageTrace(db=db, actor=actor, feature="hub-agent")
        row = trace.start(payload())
        trace.finish(row, payload=payload(), response=response(usage={}), http_status=200)
    alice = usage_report(db=db, actor="alice")
    assert alice["total"]["requests"] == alice["total"]["unknown_cost"] == 1
    assert alice["total"]["unknown_usage"] == 1
    assert alice["runs"][0]["actor"] == "alice"
    assert usage_report(db=db, actor="admin")["total"]["requests"] == 2
    with pytest.raises(HubOperationError): usage_report(db=db, actor="unknown")
    gateway = HubOperationService(db=db, cipher=SecretCipher("a" * 32), actor="alice")
    assert "runs" not in gateway.query("ai.usage.read", {})
    assert gateway.query("ai.usage.read", {"detail": "recent"})["total"]["requests"] == 1


def test_usage_page_renders_unknown_values_and_escapes_metadata(db):
    from jinja2 import Environment, FileSystemLoader
    trace = AiUsageTrace(db=db, actor="alice", feature="hub-agent")
    trace.start(payload())
    template = Environment(loader=FileSystemLoader("app/templates"), autoescape=True).get_template("partials/agent_ai_usage.html")
    html = template.render(ai_usage=usage_report(db=db, actor="alice"))
    assert "unbekannt" in html and "Nicht abgeschlossen" in html and "gpt-5.6-sol" in html
