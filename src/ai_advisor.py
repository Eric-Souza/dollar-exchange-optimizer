"""Gemini-powered second opinion on the exchange recommendation."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import requests

from src.analysis import ExchangeReport, Verdict, remaining_weekdays
from src.forecast_log import TrackRecord
from src.news import Headline
from src.ptax import DailyRate

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_URL = GEMINI_BASE + "/models/{model}:generateContent"
# Used when model discovery fails.
STATIC_MODELS = ("gemini-flash-latest", "gemini-flash-lite-latest")
MAX_CANDIDATES = 8
AI_TIME_BUDGET_SECONDS = 360
REQUEST_TIMEOUT_SECONDS = 120
RETRY_DELAY_SECONDS = 20
HISTORY_DAYS = 90
MAX_HEADLINES = 14

# Official calendars the model can read when the url_context tool is allowed.
CALENDAR_URLS = {
    "FOMC meetings": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
}

TOOL_SEARCH = "google_search"
TOOL_URL = "url_context"
TOOL_LABELS = {TOOL_SEARCH: "Google Search", TOOL_URL: "URL reading"}

# Only plain versioned models: skips previews, TTS, image, aliases, etc.
MODEL_PATTERN = re.compile(r"^gemini-(\d+(?:\.\d+)?)-(pro|flash|flash-lite)$")
TIER_RANK = {"pro": 0, "flash": 1, "flash-lite": 2}

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
- selic_target_pct and last_copom: Brazil's policy rate and latest decision. High
  Selic supports the real (carry). Copom meets roughly every 6-7 weeks.
- peer_currencies: recent moves of EUR/USD (global dollar) and EM peers. If peers
  move with BRL, the driver is global; if BRL diverges, it is Brazil-specific.
- recent_headlines: news from the last ~2 days (Brazilian FX and global dollar).
- your_track_record: how your previous 5-day outlooks turned out. If you have been
  wrong often, lower your confidence accordingly.

Give an independent second opinion. Look at the news that moves USD/BRL (Fed,
Copom/Selic, Brazilian fiscal and political news, US data, risk sentiment,
commodities) and at scheduled events in the next 5 trading days (FOMC, Copom, US
payrolls/CPI, IPCA, elections, fiscal announcements).

Also give a short-term outlook: will USD/BRL be higher ("up"), lower ("down") or
about the same ("sideways", within ±0.5%) in 5 trading days? Weigh momentum, mean
reversion toward the Focus median, the peers, the news and the event calendar.

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
    "key_events": ["up to 3 dated upcoming events, e.g. 'Oct 28 — FOMC decision'"]
  }
}"""

NO_SEARCH_NOTE = (
    "\n\nWeb search is unavailable for this request. For news, rely only on "
    "recent_headlines. Only list key_events whose dates you are certain of "
    "(from the data, the headlines or the calendars provided); otherwise return an "
    "empty list."
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
    model: str = STATIC_MODELS[0]
    tools: tuple[str, ...] = ()

    @property
    def used_search(self) -> bool:
        return TOOL_SEARCH in self.tools

    @property
    def via(self) -> str:
        return " + ".join([self.model] + [TOOL_LABELS[t] for t in self.tools])


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _track_record_summary(track: TrackRecord) -> dict[str, Any]:
    return {
        "scored_outlooks": len(track.scored),
        "correct": track.hits,
        "recent": [
            {
                "date": s.date.isoformat(),
                "predicted": s.direction,
                "confidence": s.confidence,
                "actual": s.actual,
                "change_pct": round(s.change_pct, 2),
            }
            for s in track.scored[:8]
        ],
    }


def build_prompt(
    report: ExchangeReport,
    history: list[DailyRate],
    market_context: dict[str, Any] | None = None,
    headlines: list[Headline] | None = None,
    track_record: TrackRecord | None = None,
) -> str:
    """Serialize the report, context and recent closing rates for the model."""
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
    if headlines:
        data["recent_headlines"] = [
            {
                "title": h.title,
                "source": h.source,
                "published": h.published.isoformat() if h.published else None,
            }
            for h in headlines[:MAX_HEADLINES]
        ]
    if track_record and track_record.scored:
        data["your_track_record"] = _track_record_summary(track_record)
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


def parse_gemini_response(
    payload: dict[str, Any], model: str, tools: tuple[str, ...] = ()
) -> AIOpinion:
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
        tools=tools,
    )


def rank_models(names: list[str]) -> list[str]:
    """Order models best-first: pro > flash > flash-lite, newest version first."""
    ranked = []
    for name in names:
        match = MODEL_PATTERN.match(name.removeprefix("models/"))
        if match:
            version, tier = match.groups()
            ranked.append((TIER_RANK[tier], -float(version), match.group(0)))
    return [name for *_, name in sorted(ranked)][:MAX_CANDIDATES]


def discover_models(api_key: str) -> list[str]:
    """List models this key can call; falls back to stable aliases."""
    try:
        response = requests.get(
            f"{GEMINI_BASE}/models",
            headers={"x-goog-api-key": api_key},
            params={"pageSize": 200},
            timeout=30,
        )
        response.raise_for_status()
        names = [
            m["name"]
            for m in response.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        ranked = rank_models(names)
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"Gemini model discovery failed: {exc}", file=sys.stderr)
        ranked = []
    return ranked + [m for m in STATIC_MODELS if m not in ranked]


def _tool_note(tools: tuple[str, ...]) -> str:
    note = "" if TOOL_SEARCH in tools else NO_SEARCH_NOTE
    if TOOL_URL in tools:
        calendars = "; ".join(f"{name}: {url}" for name, url in CALENDAR_URLS.items())
        note += f"\n\nRead these official calendars for upcoming meeting dates: {calendars}"
    return note


def _call_gemini(
    api_key: str, model: str, prompt: str, tools: tuple[str, ...], timeout: float
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt + _tool_note(tools)}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if tools:
        body["tools"] = [{tool: {}} for tool in tools]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"

    response = requests.post(
        GEMINI_URL.format(model=model),
        headers={"x-goog-api-key": api_key},
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def ai_enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def get_ai_opinion(
    report: ExchangeReport,
    history: list[DailyRate],
    market_context: dict[str, Any] | None = None,
    headlines: list[Headline] | None = None,
    track_record: TrackRecord | None = None,
) -> AIOpinion | None:
    """Ask the best Gemini model and tools available right now.

    Free-tier access changes day to day: some models have no quota (429), others
    are overloaded (503) or slow, and tools like Search have separate quotas. So
    we walk models best-first and, per model, drop tools that get refused, with
    an overall time budget. Returns None if disabled or nothing worked.
    """
    if not ai_enabled() or report.verdict == Verdict.NO_QUOTE:
        return None

    api_key = os.environ["GEMINI_API_KEY"].strip()
    preferred = os.environ.get("GEMINI_MODEL", "").strip()
    models = discover_models(api_key)
    if preferred:
        models = [preferred] + [m for m in models if m != preferred]

    available = {TOOL_SEARCH, TOOL_URL}
    if os.environ.get("GEMINI_USE_SEARCH", "true").strip().lower() == "false":
        available.discard(TOOL_SEARCH)

    prompt = build_prompt(report, history, market_context, headlines, track_record)
    deadline = time.monotonic() + AI_TIME_BUDGET_SECONDS
    retry_later: list[str] = []

    def try_model(model: str) -> AIOpinion | None:
        """Walk the tool ladder on one model; may queue it for a retry."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 15:
                return None
            tools = tuple(t for t in (TOOL_SEARCH, TOOL_URL) if t in available)
            try:
                payload = _call_gemini(
                    api_key, model, prompt, tools, min(REQUEST_TIMEOUT_SECONDS, remaining)
                )
                return parse_gemini_response(payload, model, tools)
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                status = _status(exc)
                print(f"Gemini {model} {tools or '(plain)'} failed: {exc}", file=sys.stderr)
                if tools and status in (400, 429):
                    # Refused tool quota/support: drop the most restricted tool, retry.
                    available.discard(tools[0])
                    continue
                if status is None or status >= 500:
                    retry_later.append(model)  # overloaded or timed out
                return None  # no quota for this model, gone, or bad reply

    for model in models:
        opinion = try_model(model)
        if opinion:
            return opinion

    for model in list(retry_later):  # second pass only; failures are not requeued
        if deadline - time.monotonic() < RETRY_DELAY_SECONDS + 15:
            break
        time.sleep(RETRY_DELAY_SECONDS)
        opinion = try_model(model)
        if opinion:
            return opinion
    return None
