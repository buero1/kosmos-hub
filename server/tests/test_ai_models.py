from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment
import pytest
from sqlalchemy import select, update

from app.api.routes import accounts
from app.core.security import SecretCipher
from app.db.session import get_db
from app.models.ai_usage import AiUsageRequest
from app.models.audit_log import AuditLog
from app.models.hub_user import HubUser
from app.services import ai_models
from app.services.ai_models import MODEL_PROFILES, ModelProfile, apply_model_options
from app.services.ai_provider import AiProviderConfigService
from app.services.ai_usage import AiUsageError, AiUsageTrace, estimate_cost, request_openai_json, usage_report
from app.services.hub_operations import HubOperationError, HubOperationService
from test_ai_usage import db, payload, response


@pytest.mark.parametrize("model,low,high,exact,long_low,long_high", [
    ("gpt-5.6-sol", ".0312", ".0332", ".0322", "2.43", "3.03"),
    ("gpt-5.6-terra", ".0176", ".0186", ".0181", "1.218", "1.518"),
    ("gpt-5.6-luna", ".00176", ".00186", ".00181", ".1218", ".1518"),
])
def test_tariffs_and_returned_snapshots(model, low, high, exact, long_low, long_high):
    for name in (model, model + "-2026-09-21"):
        assert estimate_cost(name, 10000, 8000, 1000) == (Decimal(low), Decimal(high))
        assert estimate_cost(name, 10000, 8000, 1000, 1000) == (Decimal(exact), Decimal(exact))
        assert estimate_cost(name, 300000, 0, 1000) == (Decimal(long_low), Decimal(long_high))
    assert estimate_cost(model + "-unknown", 10000, 8000, 1000) == (None, None)


@pytest.mark.parametrize("counts", [(-1, 0, 10), (100, -1, 10), (100, 0, -1),
    (100, 101, 10), (True, 0, 10), (100, False, 10), (100, 0, "10")])
def test_invalid_token_counts_are_not_free(counts):
    assert estimate_cost("gpt-5.6-terra", *counts) == (None, None)


def test_threshold_is_strict_and_cache_writes_are_only_the_premium():
    assert estimate_cost("gpt-5.6-terra", 272000, 0, 0, 0) == (Decimal(".544"),) * 2
    assert estimate_cost("gpt-5.6-terra", 272001, 0, 0, 0) == (Decimal("1.088004"),) * 2
    assert estimate_cost("gpt-5.6-luna", 1000, 1000, 0, 0) == (Decimal(".00002"),) * 2


def test_adding_a_profile_does_not_require_agent_or_meter_changes(monkeypatch):
    profile = ModelProfile(model="future-plain", label="Plain", pricing=replace(MODEL_PROFILES[0].pricing,
        input=Decimal(1), cached_input=Decimal(".1"), output=Decimal(2),
        cache_write_multiplier=Decimal(1), long_context_threshold=None))
    monkeypatch.setattr(ai_models, "MODEL_PROFILES", (profile,))
    data = {"model": profile.model, "input": [{"role": "user", "content": "Question"}]}
    apply_model_options(data, cache_namespace="actor")
    assert set(data) == {"model", "input", "service_tier"}
    assert estimate_cost(profile.model, 10000, 8000, 1000) == (Decimal(".0048"),) * 2


def test_unknown_model_never_starts_a_paid_request(monkeypatch):
    monkeypatch.setattr("app.services.ai_usage.request.urlopen", lambda *a, **k: pytest.fail("Unknown price"))
    with pytest.raises(AiUsageError, match="Modellprofil"):
        request_openai_json(api_key="test", payload={**payload(), "model": "unknown"}, timeout=1)


@pytest.mark.parametrize("feature", ["hub-agent", "email-rewrite", "wordpress-assistant"])
@pytest.mark.parametrize("failure", ["missing-usage", "unknown-model", "wrong-model", "invalid-writes", "tier"])
def test_uncertain_cost_stops_followup_calls_for_all_features(db, feature, failure):
    trace = AiUsageTrace(db=db, actor="admin", feature=feature)
    identifier = trace.start(payload())
    data = response()
    if failure == "missing-usage": data["usage"] = {}
    if failure == "unknown-model": data["model"] = "unknown"
    if failure == "wrong-model": data["model"] = "gpt-5.6-terra"
    if failure == "invalid-writes": data["usage"]["input_tokens_details"]["cache_write_tokens"] = -1
    if failure == "tier": data["service_tier"] = "priority"
    trace.finish(identifier, payload=payload(), response=data)
    assert db.get(AiUsageRequest, identifier).estimated_usd is None
    with pytest.raises(AiUsageError, match="unbekannt"):
        trace.start(payload())


def test_cost_snapshot_is_frozen_before_request_and_history_never_reprices(db, monkeypatch):
    trace = AiUsageTrace(db=db, actor="admin", feature="hub-agent")
    identifier = trace.start(payload())
    original = MODEL_PROFILES[0]
    monkeypatch.setattr(ai_models, "MODEL_PROFILES", (replace(original,
        pricing=replace(original.pricing, input=Decimal(999), output=Decimal(999), version="future-tariff")),))
    trace.finish(identifier, payload=payload(), response=response())
    row = db.get(AiUsageRequest, identifier)
    assert row.estimated_usd == Decimal(".0312")
    assert row.pricing_version == original.pricing.version
    assert row.sizes["pricing"]["input"] == "4"
    report = usage_report(db=db, actor="admin")
    assert report["total"]["usd"] == "0.0312"
    assert report["runs"][0]["calls"][0]["pricing_version"] == row.pricing_version


def test_budget_guard_blocks_network_before_new_request(db, monkeypatch):
    trace = AiUsageTrace(db=db, actor="admin", feature="wordpress-assistant")
    trace.estimated_upper = Decimal("1")
    monkeypatch.setattr("app.services.ai_usage.request.urlopen", lambda *a, **k: pytest.fail("No remaining budget"))
    with pytest.raises(AiUsageError, match="Kostenschutzgrenze"):
        request_openai_json(api_key="test", payload=payload(), timeout=1, trace=trace)
    assert db.scalars(select(AiUsageRequest)).all() == []


@pytest.mark.parametrize("profile", MODEL_PROFILES)
@pytest.mark.parametrize("feature", ["email-rewrite", "wordpress-assistant"])
def test_other_ai_features_use_the_same_model_policy(profile, feature, monkeypatch):
    from app.services.ai_assistant import HubAssistantService
    from app.services.email_ai_rewrite import EmailAiRewriteService
    captured = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"output":[]}'
    def fake(req, **kwargs):
        captured.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr("app.services.ai_usage.request.urlopen", fake)
    if feature == "email-rewrite":
        object.__new__(EmailAiRewriteService)._create_openai_response(api_key="test", model=profile.model,
            instruction="Improve", selected_html="<p>Test</p>")
    else:
        object.__new__(HubAssistantService)._create_openai_response(api_key="test", model=profile.model,
            input_items=[{"role": "user", "content": "Test"}])
    assert captured[0]["model"] == profile.model
    assert captured[0]["reasoning"] == {"effort": "low"}
    assert captured[0]["include"] == ["reasoning.encrypted_content"]
    assert captured[0]["service_tier"] == "default"


def configure(db):
    user = db.scalar(select(HubUser).where(HubUser.username == "admin"))
    cipher = SecretCipher("a" * 32)
    provider = AiProviderConfigService(db=db, cipher=cipher)
    config = provider.configure_openai(actor=user, api_key="sk-private-test-abcdefghijklmnopqrstuvwxyz")
    db.commit()
    return user, cipher, provider, config


def test_shared_model_selection_preserves_key_and_survives_key_rotation(db):
    user, cipher, provider, config = configure(db)
    gateway = HubOperationService(db=db, cipher=cipher, actor="admin")
    before_key = config.encrypted_api_key
    for profile in reversed(MODEL_PROFILES):
        gateway.execute("settings.ai_model.update", {"model": profile.model})
        assert config.model == profile.model and config.encrypted_api_key == before_key
    gateway.execute("settings.ai_model.update", {"model": "gpt-5.6-terra"})
    provider.configure_openai(actor=user, api_key="sk-replaced-test-abcdefghijklmnopqrstuvwxyz")
    db.commit()
    assert provider.get_enabled_openai_api_key()[0].model == "gpt-5.6-terra"
    visible = gateway.query("settings.read", {"section": "ai_model"})
    assert len(visible["models"]) == 3 and visible["model"] == "gpt-5.6-terra"
    assert "sk-private" not in json.dumps(visible) and "encrypted_api_key" not in json.dumps(visible)
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "select-openai-model")).all()
    assert len(audit) == 4 and "gpt-5.6-terra" in audit[-1].detail


def test_shared_model_setting_revalidates_permissions_and_rejects_unknown_models(db):
    user, cipher, provider, config = configure(db)
    gateway = HubOperationService(db=db, cipher=cipher, actor="admin")
    with pytest.raises(HubOperationError):
        gateway.execute("settings.ai_model.update", {"model": "unknown"})
    assert config.model == "gpt-5.6-sol"
    db.execute(update(HubUser).where(HubUser.id == user.id).values(role="viewer").execution_options(synchronize_session=False))
    for action in (lambda: gateway.execute("settings.ai_model.update", {"model": "gpt-5.6-terra"}),
                   lambda: gateway.query("settings.read", {"section": "ai_model"})):
        with pytest.raises(HubOperationError, match="Berechtigung"): action()
    assert config.model == "gpt-5.6-sol"


def test_model_route_uses_shared_operation_and_csrf_without_a_new_key(db, monkeypatch):
    user, cipher, provider, config = configure(db)
    monkeypatch.setattr(accounts, "get_secret_cipher", lambda: cipher)
    app = FastAPI()
    app.include_router(accounts.router)
    app.dependency_overrides[get_db] = lambda: db
    @app.middleware("http")
    async def identity(req, call_next):
        req.state.hub_user = user
        req.scope["session"] = {"csrf_token": "test-csrf"}
        return await call_next(req)
    with TestClient(app) as client:
        denied = client.post("/account/openai/model", data={"model": "gpt-5.6-luna"})
        assert denied.status_code == 403 and config.model == "gpt-5.6-sol"
        saved = client.post("/account/openai/model", data={"model": "gpt-5.6-terra", "csrf_token": "test-csrf"}, follow_redirects=False)
        assert saved.status_code == 303 and "model-saved" in saved.headers["location"]
    assert config.model == "gpt-5.6-terra"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "select-openai-model")) is not None


def test_model_selector_renders_existing_choice_rates_and_csrf(db):
    user, cipher, provider, config = configure(db)
    provider.select_openai_model(actor=user, model="gpt-5.6-terra")
    source = Path("app/templates/account.html").read_text(encoding="utf-8")
    form = '<form method="post" action="/account/openai/model">' + source.split('<form method="post" action="/account/openai/model">')[1].split('</form>')[0] + '</form>'
    html = Environment(autoescape=True).from_string(form).render(openai_config=config, openai_models=MODEL_PROFILES, csrf_token="test-csrf")
    assert '<option value="gpt-5.6-terra" selected>' in html
    assert '<option value="gpt-5.6-sol" selected>' not in html
    assert 'name="csrf_token" value="test-csrf"' in html
    assert 'name="api_key"' not in html
    assert "0.02" in html and "23.09.2026" in html
