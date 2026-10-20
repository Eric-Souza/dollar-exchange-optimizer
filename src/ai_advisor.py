"""Gemini-powered second opinion on the exchange recommendation."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import requests

from src.analysis import ExchangeReport, Verdict, remaining_weekdays
from src.ptax import DailyRate

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
FALLBACK_MODELS = ("gemini-flash-lite-latest",)
ATTEMPTS_PER_MODEL = 2
RETRY_DELAY_SECONDS = 15
REQUEST_TIMEOUT_SECONDS = 120
HISTORY_DAYS = 90

SYSTEM_INSTRUCTION = """\
You advise a person in Brazil who receives a monthly salary in USD (usually in the
first days of the month) and converts it all to BRL once per month. Their goal is
the highest BRL-per-USD rate within the month. They exchange via a bank, so the
PTAX buy rate (cotacaoCompra) is the reference.

You get a rule-based verdict computed from percentiles, raw rates and signals, plus
market context when available:
- focus_survey_usdbrl_by_month: BACEN Focus survey, the median USD/BRL that ~100
  banks and economists expect for each month. A median well below today's rate means
  the market expects the dollar to weaken (favoring exchanging now), and vice versa.
- selic_target_pct: Brazil's policy rate. High Selic supports the real (carry).
- peer_currencies: recent moves of EUR/USD (global dollar) and EM peers. If peers
  move with BRL, the driver is global; if BRL diverges, it is Brazil-specific.

Give an independent second opinion. If you have web search, check the last few days
of news that move USD/BRL (Fed, Copom/Selic, Brazilian fiscal news, US data, risk
sentiment, commodities) and look up scheduled events in the next 5 trading days
(FOMC, Copom, US payrolls/CPI, IPCA, fiscal announcements).

Also give a short-term outlook: will USD/BRL be higher ("up"), lower ("down") or
about the same ("sideways", within roughly one day's typical move) in 5 trading
days? Weigh momentum, mean reversion toward the Focus median, the peers and the
event calendar.

Be calibrated: short-term FX direction is close to a coin flip even for
professionals. Use "high" confidence only with strong, converging evidence; "low"
is the honest default. Do not invent news or events; if you found none, say so.

Respond with ONLY a JSON object, no markdown fences:
{
  "verdict": "EXCHANGE TODAY" | "GOOD DAY" | "NEUTRAL" | "WAIT",
  "confidence": "low" | "medium" | "high",
  "summary": "2-3 sentences, plain language, the key reason for your verdict",
  "drivers": ["up to 3 short bullet points: what is moving the rate"],
  "risks": ["up to 3 short bullet points: what could make your call wrong"],
  "outlook": {
    "direction": "up" | "down" | "sideways",
    "confidence": "low" | "medium" | "high",
    "reasoning": "1-2 sentences",
    "key_events": ["up to 3 dated upcoming events, e.g. 'Oct 28 — Copom decision'"]
  }
}"""

NO_SEARCH_NOTE = (
    "\n\nWeb search is unavailable for this request. Do not cite news. Only list "
    "key_events whose dates you are certain of (e.g. published FOMC/Copom calendars); "
    "otherwise return an empty list."
)

AI_VERDICTS = {v.value: v for v in Verdict if v != Verdict.NO_QUOTE}
CONFIDENCE_LEVELS = ("low", "medium", "high")
DIRECTIONS = ("up", "down", "sideways")


@dataclass(frozen=True)
class Outlook:
    """AI's guess at USD/BRL direction over the next 5 trading days."""

    direction: str
    confidence: str | None
    reasoning: str
    key_events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AIOpinion:
    verdict: Verdict | None
    confidence: str | None
    summary: str
    drivers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    outlook: Outlook | None = None
    sources: list[tuple[str, str]] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    used_search: bool = False


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def build_prompt(
    report: ExchangeReport,
    history: list[DailyRate],
    market_context: dict[str, Any] | None = None,
) -> str:
    """Serialize the report, market context and recent closing rates for the model."""
    ref = report.reference_date
    cutoff = ref - timedelta(days=HISTORY_DAYS)
    signals = report.signals
    stats = report.month_stats

    data: dict[str, Any] = {
        "today": ref.isoformat(),
        "weekday": ref.strftime("%A"),
        "weekdays_left_in_month": remaining_weekdays(ref),
        "today_rate": report.today_rate,
        "today_rate_source": report.rate_source,
        "today_rate_is_final_closing": report.rate_is_final,
        "rule_based_verdict": report.verdict.value,
        "rule_based_explanation": report.explanation,
        "percentile_30d": _round(report.rolling_30d_percentile, 1),
        "percentile_month": _round(report.month_percentile, 1),
        "month_best": (
            {"date": stats.best_date.isoformat(), "rate": stats.best_rate} if stats else None
        ),
        "month_average": _round(stats.average, 4) if stats else None,
    }
    if signals:
        data["signals"] = {
            "percentile_90d": _round(signals.quarter_percentile, 1),
            "zscore_30d": _round(signals.zscore_30d),
            "ma5": _round(signals.ma5, 4),
            "ma20": _round(signals.ma20, 4),
            "trend_ma5_vs_ma20": signals.trend,
            "change_1d_pct": _round(signals.change_1d_pct),
            "change_5d_pct": _round(signals.change_5d_pct),
            "daily_volatility_20d_pct": _round(signals.volatility_20d_pct),
        }
    if market_context:
        data["market_context"] = market_context
    data["ptax_closing_history"] = [
        [r.date.isoformat(), r.rate] for r in history if cutoff <= r.date < ref
    ]
    return "Today's data:\n" + json.dumps(data, ensure_ascii=False)


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply (tolerates fences and prose)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"No JSON object in model reply: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:3]


def _confidence(value: Any) -> str | None:
    level = str(value or "").lower()
    return level if level in CONFIDENCE_LEVELS else None


def _parse_outlook(value: Any) -> Outlook | None:
    if not isinstance(value, dict):
        return None
    direction = str(value.get("direction", "")).lower()
    if direction not in DIRECTIONS:
        return None
    return Outlook(
        direction=direction,
        confidence=_confidence(value.get("confidence")),
        reasoning=str(value.get("reasoning", "")).strip(),
        key_events=_str_list(value.get("key_events")),
    )


def parse_gemini_response(payload: dict[str, Any], model: str, used_search: bool) -> AIOpinion:
    candidate = payload["candidates"][0]
    text = "".join(part.get("text", "") for part in candidate["content"]["parts"])
    data = _extract_json(text)

    sources: list[tuple[str, str]] = []
    for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []):
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append((web.get("title") or web["uri"], web["uri"]))

    return AIOpinion(
        verdict=AI_VERDICTS.get(str(data.get("verdict", "")).upper()),
        confidence=_confidence(data.get("confidence")),
        summary=str(data.get("summary", "")).strip(),
        drivers=_str_list(data.get("drivers")),
        risks=_str_list(data.get("risks")),
        outlook=_parse_outlook(data.get("outlook")),
        sources=sources[:5],
        model=model,
        used_search=used_search,
    )


def _call_gemini(api_key: str, model: str, prompt: str, use_search: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [
            {"role": "user", "parts": [{"text": prompt if use_search else prompt + NO_SEARCH_NOTE}]}
        ],
        "generationConfig": {"temperature": 0.2},
    }
    if use_search:
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"

    response = requests.post(
        GEMINI_URL.format(model=model),
        headers={"x-goog-api-key": api_key},
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def ai_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def get_ai_opinion(
    report: ExchangeReport,
    history: list[DailyRate],
    market_context: dict[str, Any] | None = None,
) -> AIOpinion | None:
    """Ask Gemini for a second opinion. Returns None if disabled or on any failure."""
    if not ai_enabled() or report.verdict == Verdict.NO_QUOTE:
        return None

    api_key = os.environ["GEMINI_API_KEY"].strip()
    primary = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    models = [primary] + [m for m in FALLBACK_MODELS if m != primary]
    search_ok = os.environ.get("GEMINI_USE_SEARCH", "true").strip().lower() != "false"
    prompt = build_prompt(report, history, market_context)

    # Free-tier models are often overloaded (503) or slow, so retry and then
    # fall back to a lighter model. Search grounding has its own quota: once
    # it's refused (429), stop asking for it.
    for model in models:
        for attempt in range(ATTEMPTS_PER_MODEL):
            if attempt:
                time.sleep(RETRY_DELAY_SECONDS)
            use_search = search_ok
            try:
                payload = _call_gemini(api_key, model, prompt, use_search)
                return parse_gemini_response(payload, model, use_search)
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                print(f"Gemini {model} failed (search={use_search}): {exc}", file=sys.stderr)
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if use_search and status in (400, 429):
                    search_ok = False
    return None
