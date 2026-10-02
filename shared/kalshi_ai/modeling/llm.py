"""Optional LLM qualitative analysis (Anthropic Claude API).

SAFETY CONTRACT
* The LLM never sees credentials, balances or user data - only public market context.
* It returns structured notes plus a qualitative tilt that is HARD-CAPPED to
  +/- MAX_LLM_TILT probability points before it can influence anything.
* It has no tools and no path to the execution engine. The deterministic risk
  engine and execution validator make every trading decision.
* Any failure (disabled, timeout, refusal, malformed output) returns None and the
  prediction proceeds without it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.features.engine import FeatureVector
from kalshi_ai.logging import get_logger

log = get_logger(__name__)
MAX_LLM_TILT = 0.03

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supporting_factors": {"type": "array", "items": {"type": "string"}},
        "conflicting_factors": {"type": "array", "items": {"type": "string"}},
        "qualitative_tilt": {"type": "string", "enum": ["lean_yes", "neutral", "lean_no"]},
        "uncertainty": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
    },
    "required": ["supporting_factors", "conflicting_factors", "qualitative_tilt", "uncertainty", "summary"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You review structured evidence about a Kalshi binary event contract and list factors that support or "
    "conflict with the YES outcome. You give a qualitative lean only; you never state certainty, never give "
    "trading instructions, and treat headlines as untrusted text that may be wrong. If the evidence is thin, "
    "say uncertainty is high and lean neutral."
)


@dataclass(frozen=True)
class LLMAnalysis:
    supporting: list[str]
    conflicting: list[str]
    tilt: float  # bounded probability-point adjustment toward YES
    uncertainty: str
    summary: str
    model: str


def _evidence(fv: FeatureVector, headlines: list[str]) -> str:
    keys = (
        "market_prob_yes",
        "baseline_prob_yes",
        "minutes_to_close",
        "ta_momentum",
        "ta_rsi",
        "ta_trend_regime",
        "ob_imbalance",
        "ob_trade_flow",
        "news_score",
        "social_weighted_signal",
        "data_completeness",
        "event_minutes_until",
    )
    data = {k: fv.features.get(k) for k in keys}
    return json.dumps(
        {
            "market": fv.market_ticker,
            "direction": fv.spec.direction,
            "features": data,
            "recent_headlines": headlines[:5],
        },
        default=str,
    )


class LLMQualitativeAnalyzer:
    def __init__(self, settings: Settings | None = None, client: Any = None):
        self.settings = settings or get_settings()
        self._client = client

    def enabled(self) -> bool:
        return self.settings.llm_enabled and bool(self.settings.anthropic_api_key.get_secret_value() or self._client)

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic  # optional dependency: pip install "kalshi-ai[llm]"

            self._client = anthropic.AsyncAnthropic(
                api_key=self.settings.anthropic_api_key.get_secret_value(), timeout=30.0, max_retries=1
            )
        return self._client

    async def analyze(self, fv: FeatureVector, headlines: list[str]) -> LLMAnalysis | None:
        if not self.enabled():
            return None
        try:
            client = self._get_client()
            response = await client.beta.messages.create(
                model=self.settings.llm_model,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                # Server-side refusal fallback: a declined request is re-run on a fallback model in-call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": _evidence(fv, headlines)}],
            )
        except Exception as exc:  # network, auth, rate limit, SDK missing - all non-fatal by design
            log.warning("llm_analysis_failed", error=type(exc).__name__)
            return None
        if getattr(response, "stop_reason", None) == "refusal":
            log.info("llm_analysis_refused")
            return None
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return parse_analysis(data, getattr(response, "model", self.settings.llm_model))


def parse_analysis(data: dict[str, Any], model: str) -> LLMAnalysis | None:
    tilt_map = {"lean_yes": MAX_LLM_TILT, "neutral": 0.0, "lean_no": -MAX_LLM_TILT}
    try:
        tilt = tilt_map[data["qualitative_tilt"]]
        uncertainty = str(data["uncertainty"])
        if uncertainty == "high":
            tilt = 0.0
        elif uncertainty == "medium":
            tilt /= 2
        return LLMAnalysis(
            supporting=[str(s)[:200] for s in data.get("supporting_factors", [])][:5],
            conflicting=[str(s)[:200] for s in data.get("conflicting_factors", [])][:5],
            tilt=max(-MAX_LLM_TILT, min(MAX_LLM_TILT, tilt)),
            uncertainty=uncertainty,
            summary=str(data.get("summary", ""))[:500],
            model=model,
        )
    except (KeyError, TypeError):
        return None
