"""Reviewed model capabilities and tariffs; application logic must not branch on model IDs."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
import re


class AiModelError(ValueError):
    pass


@dataclass(frozen=True)
class TokenPricing:
    version: str
    source: str
    input: Decimal
    cached_input: Decimal
    output: Decimal
    cache_write_multiplier: Decimal = Decimal("1")
    long_context_threshold: int | None = None
    long_input_multiplier: Decimal = Decimal("1")
    long_output_multiplier: Decimal = Decimal("1")

    def snapshot(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class ModelProfile:
    model: str
    label: str
    pricing: TokenPricing
    reasoning_effort: str | None = None
    encrypted_reasoning: bool = False
    explicit_cache: bool = False
    dated_snapshots: bool = False

    def matches(self, model):
        return isinstance(model, str) and (model == self.model or bool(self.dated_snapshots and
            re.fullmatch(re.escape(self.model) + r"-\d{4}-\d{2}-\d{2}", model)))


def _gpt56(name, input_price, cached_price, output_price):
    model = f"gpt-5.6-{name.lower()}"
    return ModelProfile(model=model, label=f"GPT-5.6 {name}",
        pricing=TokenPricing(version=f"openai-standard-2026-09-23-{name.lower()}-v1",
            source=f"https://developers.openai.com/api/docs/models/{model}",
            input=Decimal(input_price), cached_input=Decimal(cached_price), output=Decimal(output_price),
            cache_write_multiplier=Decimal("1.25"), long_context_threshold=272_000,
            long_input_multiplier=Decimal("2"), long_output_multiplier=Decimal("1.5")),
        reasoning_effort="low", encrypted_reasoning=True, explicit_cache=True, dated_snapshots=True)


MODEL_PROFILES = (
    _gpt56("Sol", "4", "0.40", "20"),
    _gpt56("Terra", "2", "0.20", "12"),
    _gpt56("Luna", "0.20", "0.02", "1.20"),
)
DEFAULT_OPENAI_MODEL = MODEL_PROFILES[0].model


def model_profile(model):
    return next((profile for profile in MODEL_PROFILES if profile.matches(model)), None)


def require_model_profile(model):
    profile = model_profile(model)
    if profile is None:
        raise AiModelError("Kein freigegebenes Modellprofil mit bekannter Preisgrundlage. Bitte unter Account > OpenAI ein Modell auswaehlen.")
    return profile


def apply_model_options(payload, *, cache_namespace=None):
    """Set only supported options. Optional prefix caching is independent of model names."""
    profile = require_model_profile(payload.get("model"))
    # Explicitly request standard processing, not an account-level premium default.
    payload["service_tier"] = "default"
    if profile.reasoning_effort is not None:
        payload["reasoning"] = {"effort": profile.reasoning_effort}
    else:
        payload.pop("reasoning", None)
    include = [value for value in payload.get("include", []) if value != "reasoning.encrypted_content"]
    if profile.encrypted_reasoning:
        include.append("reasoning.encrypted_content")
    if include:
        payload["include"] = include
    else:
        payload.pop("include", None)
    if profile.explicit_cache and cache_namespace is not None:
        payload["prompt_cache_options"] = {"mode": "explicit"}
        payload["prompt_cache_key"] = sha256(cache_namespace.encode()).hexdigest()
        # Reusable tools and documents precede the changing user question/history.
        if len(payload["input"]) > 1 and len(payload["input"][0]["content"][0]["text"]) >= 4096:
            payload["input"][0]["content"][0]["prompt_cache_breakpoint"] = {"mode": "explicit"}
        payload["input"].insert(0, {"role": "developer", "content": [{"type": "input_text",
            "text": "Die Grundregeln und Werkzeugdefinitionen bleiben unveraendert gueltig.",
            "prompt_cache_breakpoint": {"mode": "explicit"}}]})
    elif not profile.explicit_cache:
        payload.pop("prompt_cache_options", None)
        payload.pop("prompt_cache_key", None)
    return profile
