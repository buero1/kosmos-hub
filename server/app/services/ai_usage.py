"""Meter every HTTP attempt without storing prompts, documents or credentials."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import re
from time import monotonic
from urllib import error, request
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.ai_usage import AiUsageRequest
from app.models.hub_user import HubUser
from app.services.ai_models import AiModelError, apply_model_options, model_profile, require_model_profile

PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"


class AiUsageError(ValueError):
    pass


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def estimate_cost(model, input_tokens, cached_tokens, output_tokens, cache_write_tokens=None, *, profile=None):
    # Cache writes are not separately reported by every response. Show a range
    # instead of pretending every non-cached input token has the same tariff.
    profile = profile or model_profile(model)
    if profile is None or not profile.matches(model):
        return None, None
    if any(_count(value) is None for value in (input_tokens, cached_tokens, output_tokens)) or cached_tokens > input_tokens:
        return None, None
    price = profile.pricing
    long_context = price.long_context_threshold is not None and input_tokens > price.long_context_threshold
    input_multiplier = price.long_input_multiplier if long_context else Decimal(1)
    output_multiplier = price.long_output_multiplier if long_context else Decimal(1)
    uncached = Decimal(input_tokens - cached_tokens) * price.input * input_multiplier
    cached = Decimal(cached_tokens) * price.cached_input * input_multiplier
    output = Decimal(output_tokens) * price.output * output_multiplier
    if cache_write_tokens is not None:
        if _count(cache_write_tokens) is None or cache_write_tokens > input_tokens - cached_tokens:
            return None, None
        write_premium = Decimal(cache_write_tokens) * price.input * (price.cache_write_multiplier - 1) * input_multiplier
        cost = (uncached + cached + output + write_premium) / 1_000_000
        return cost, cost
    return ((uncached + cached + output) / 1_000_000,
            (uncached * price.cache_write_multiplier + cached + output) / 1_000_000)


class AiUsageTrace:
    def __init__(self, *, db, actor, feature, conversation_id=None):
        self.engine = db.get_bind()
        self.actor = actor or ""
        self.feature = feature
        self.conversation_id = conversation_id
        self.run_id = str(uuid4())
        self.sequence = 0
        self.estimated_upper = Decimal(0)
        self.context_sizes = {}
        self.has_unknown_cost = False
        self._request_profiles = {}

    def start(self, payload):
        from app.core.config import get_settings
        if self.has_unknown_cost or self._request_profiles:
            raise AiUsageError("Kosten des vorherigen Modellaufrufs sind unbekannt. Aus Kostenschutz wird dieser Ablauf nicht automatisch fortgesetzt.")
        if self.estimated_upper >= Decimal(str(get_settings().ai_agent_run_cost_limit_usd)):
            raise AiUsageError("Die Kostenschutzgrenze dieser Anfrage wurde erreicht. Es wird kein weiterer Modellaufruf gestartet.")
        try:
            profile = require_model_profile(payload.get("model"))
        except AiModelError as exc:
            raise AiUsageError(str(exc)) from exc
        self.sequence += 1
        sizes = {name + "_chars": len(json.dumps(payload.get(name, ""), ensure_ascii=False))
                 for name in ("instructions", "tools", "input")}
        sizes.update(self.context_sizes)
        sizes["pricing"] = profile.pricing.snapshot()
        sizes["requested_model"] = payload["model"]
        # Separate transaction: rejected plans and request rollbacks still cost money.
        with Session(self.engine) as meter:
            row = AiUsageRequest(run_id=self.run_id, sequence=self.sequence, actor=self.actor,
                feature=self.feature, conversation_id=self.conversation_id,
                started_at=datetime.now(UTC).replace(tzinfo=None), model=payload.get("model", "unknown"),
                status="started", sizes=sizes, tools_used=[], pricing_version=profile.pricing.version)
            meter.add(row)
            meter.commit()
            self._request_profiles[row.id] = profile
            return row.id

    def finish(self, row_id, *, payload, response=None, elapsed_ms=0, http_status=None,
               error_code=None, request_id=None):
        response = response if isinstance(response, dict) else {}
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        inputs = usage.get("input_tokens_details") or {}
        outputs = usage.get("output_tokens_details") or {}
        with Session(self.engine) as meter:
            row = meter.get(AiUsageRequest, row_id)
            row.http_status = http_status
            row.elapsed_ms = elapsed_ms
            row.status = "error" if error_code or response.get("status") == "failed" else (
                "incomplete" if response.get("status") == "incomplete" else "completed")
            row.error_code = error_code
            row.provider_request_id = str(request_id)[:128] if request_id else None
            row.model = str(response.get("model") or row.model)[:100]
            row.input_tokens = _count(usage.get("input_tokens"))
            row.cached_tokens = _count(inputs.get("cached_tokens")) if isinstance(inputs, dict) else None
            row.output_tokens = _count(usage.get("output_tokens"))
            row.reasoning_tokens = _count(outputs.get("reasoning_tokens")) if isinstance(outputs, dict) else None
            write_tokens = _count(inputs.get("cache_write_tokens")) if isinstance(inputs, dict) else None
            row.sizes = {**row.sizes, "cache_write_tokens": write_tokens}
            profile = self._request_profiles.get(row_id)
            # Keep the tariff captured before the request, even if the registry changes.
            valid_tier = response.get("service_tier") in (None, "default")
            raw_writes = inputs.get("cache_write_tokens") if isinstance(inputs, dict) else None
            row.estimated_usd, row.estimated_usd_upper = estimate_cost(
                row.model, row.input_tokens, row.cached_tokens, row.output_tokens, raw_writes,
                profile=profile) if profile and valid_tier else (None, None)
            if row.estimated_usd is not None:
                self.estimated_upper += row.estimated_usd_upper
            else:
                self.has_unknown_cost = True
            allowed = {tool.get("name") for tool in payload.get("tools", []) if isinstance(tool, dict)}
            output_items = response.get("output")
            output_items = output_items if isinstance(output_items, list) else []
            row.tools_used = [item["name"] for item in output_items
                if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") in allowed]
            meter.commit()
            self._request_profiles.pop(row_id, None)


def request_openai_json(*, api_key, payload, timeout, trace=None):
    payload = dict(payload)
    try:
        apply_model_options(payload)
    except AiModelError as exc:
        raise AiUsageError(str(exc)) from exc
    try:
        identifier = trace.start(payload) if trace else None
    except SQLAlchemyError as exc:
        raise AiUsageError("Verbrauchserfassung nicht erreichbar. Es wurde keine KI-Anfrage gestartet.") from exc
    started = monotonic()
    data = None
    status = None
    request_id = None
    failure = None
    http_request = request.Request("https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            headers = getattr(response, "headers", {})
            request_id = headers.get("x-request-id")
            data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, dict):
                failure = "invalid_response"
        return data
    except error.HTTPError as exc:
        status = exc.code
        request_id = exc.headers.get("x-request-id") if exc.headers else None
        failure = f"http_{exc.code}"
        try:
            code = json.loads(exc.read(65536)).get("error", {}).get("code")
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,70}", code):
                failure = code
        except (ValueError, AttributeError, TypeError):
            pass
        raise
    except Exception as exc:
        failure = type(exc).__name__[:80]
        raise
    finally:
        if trace:
            try:
                trace.finish(identifier, payload=payload, response=data, http_status=status,
                    elapsed_ms=round((monotonic() - started) * 1000), error_code=failure, request_id=request_id)
            except SQLAlchemyError as exc:
                raise AiUsageError("KI-Anfrage beendet, Verbrauch konnte nicht abgeschlossen werden. Der Aufruf bleibt mit unbekannten Kosten erfasst; bitte nicht automatisch wiederholen.") from exc


def usage_report(*, db, actor):
    from app.core.config import get_settings
    from app.services.hub_operations import HubOperationError
    user = db.scalar(select(HubUser).where(HubUser.username == actor, HubUser.is_active.is_(True)))
    if user is None:
        raise HubOperationError("Kein Zugriff auf die Verbrauchsdaten.")
    now = datetime.now(UTC)
    local_today = now.astimezone(ZoneInfo("Europe/Berlin")).date()
    since = datetime.combine(local_today - timedelta(days=29), datetime.min.time(),
        tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC).replace(tzinfo=None)
    query = select(AiUsageRequest).where(AiUsageRequest.started_at >= since).order_by(AiUsageRequest.id.desc())
    if user.role != "admin":
        query = query.where(AiUsageRequest.actor == actor)
    rows = db.scalars(query.limit(10001)).all()
    limited = len(rows) > 10000
    rows = rows[:10000]

    def aggregate(entries):
        known = [entry for entry in entries if entry.estimated_usd is not None]
        return {"requests": len(entries), "runs": len({entry.run_id for entry in entries}),
            "input_tokens": sum(entry.input_tokens or 0 for entry in entries),
            "cached_tokens": sum(entry.cached_tokens or 0 for entry in entries),
            "output_tokens": sum(entry.output_tokens or 0 for entry in entries),
            "reasoning_tokens": sum(entry.reasoning_tokens or 0 for entry in entries),
            "unknown_usage": sum(entry.input_tokens is None or entry.output_tokens is None for entry in entries),
            "unknown_cost": len(entries) - len(known),
            "usd": format(sum((entry.estimated_usd for entry in known), Decimal(0)), ".4f"),
            "usd_upper": format(sum((entry.estimated_usd_upper for entry in known), Decimal(0)), ".4f")}

    groups = {}
    days = {}
    for row in rows:
        day = row.started_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        days.setdefault(day, []).append(row)
        groups.setdefault(row.run_id, []).append(row)
    runs = []
    for run_id, entries in list(groups.items())[:50]:
        first = min(entries, key=lambda row: row.sequence)
        runs.append({"run_id": run_id, "actor": first.actor, "feature": first.feature,
            "conversation_id": first.conversation_id,
            "time": first.started_at.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M:%S"),
            **aggregate(entries), "calls": [{
                "sequence": row.sequence, "model": row.model, "status": row.status,
                "error_code": row.error_code, "elapsed_ms": row.elapsed_ms,
                "input_tokens": row.input_tokens, "cached_tokens": row.cached_tokens,
                "output_tokens": row.output_tokens, "reasoning_tokens": row.reasoning_tokens,
                "usd": format(row.estimated_usd, ".4f") if row.estimated_usd is not None else None,
                "usd_upper": format(row.estimated_usd_upper, ".4f") if row.estimated_usd_upper is not None else None,
                "sizes": row.sizes, "tools_used": row.tools_used,
                "pricing_version": row.pricing_version,
            } for row in sorted(entries, key=lambda row: row.sequence)]})
    return {"scope": "Alle Benutzer" if user.role == "admin" else "Eigener Verbrauch", "limited": limited,
        "run_cost_limit_usd": get_settings().ai_agent_run_cost_limit_usd,
        "total": aggregate(rows), "days": [{"day": day, **aggregate(entries)} for day, entries in days.items()],
        "runs": runs, "price_source": PRICE_SOURCE,
        "models": [{"model": name, **aggregate([row for row in rows if row.model == name])}
                   for name in sorted({row.model for row in rows})],
        "features": [{"feature": name, **aggregate([row for row in rows if row.feature == name])}
                     for name in sorted({row.feature for row in rows})]}
