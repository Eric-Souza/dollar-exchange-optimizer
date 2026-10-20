"""Tests for the Gemini second-opinion integration."""

import json
from datetime import date

import pytest
import requests

from src import ai_advisor
from src.ai_advisor import build_prompt, get_ai_opinion, parse_gemini_response
from src.analysis import Verdict, build_report
from src.email_builder import build_email_body, build_email_html, build_email_subject
from src.ptax import DailyRate

REPLY = {
    "verdict": "WAIT",
    "confidence": "medium",
    "summary": "Copom meets next week; a hawkish surprise could strengthen the real.",
    "drivers": ["Fed cut expectations", "Fiscal noise"],
    "risks": ["Risk-off selloff"],
}


def _report():
    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]
    rolling.append(DailyRate(date(2026, 8, 5), 5.45))
    return build_report(rolling, reference=date(2026, 8, 5)), rolling


def _payload(text: str, grounding: dict | None = None) -> dict:
    candidate = {"content": {"parts": [{"text": text}]}}
    if grounding:
        candidate["groundingMetadata"] = grounding
    return {"candidates": [candidate]}


class _Response:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(ai_advisor, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_USE_SEARCH", raising=False)


def test_parse_gemini_response_with_fences_and_sources():
    text = "```json\n" + json.dumps(REPLY) + "\n```"
    grounding = {"groundingChunks": [{"web": {"uri": "https://example.com/a", "title": "Valor"}}]}

    opinion = parse_gemini_response(_payload(text, grounding), "gemini-x", used_search=True)

    assert opinion.verdict == Verdict.WAIT
    assert opinion.confidence == "medium"
    assert opinion.drivers == ["Fed cut expectations", "Fiscal noise"]
    assert opinion.sources == [("Valor", "https://example.com/a")]
    assert opinion.used_search


def test_parse_gemini_response_unknown_verdict():
    reply = dict(REPLY, verdict="SELL EVERYTHING", confidence="extreme")
    opinion = parse_gemini_response(_payload(json.dumps(reply)), "gemini-x", used_search=False)
    assert opinion.verdict is None
    assert opinion.confidence is None


def test_build_prompt_contains_data():
    report, history = _report()
    prompt = build_prompt(report, history)
    data = json.loads(prompt.split("\n", 1)[1])

    assert data["today_rate"] == 5.45
    assert data["rule_based_verdict"] == report.verdict.value
    assert ["2026-07-31", 5.30] in data["ptax_closing_history"]
    assert all(day != "2026-08-05" for day, _ in data["ptax_closing_history"])


def test_get_ai_opinion_disabled_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report, history = _report()
    assert get_ai_opinion(report, history) is None


def test_get_ai_opinion_falls_back_without_search(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json)
        if "tools" in json:
            return _Response(status=429)
        return _Response(_payload(ai_advisor.json.dumps(REPLY)))

    monkeypatch.setattr(ai_advisor.requests, "post", fake_post)
    report, history = _report()

    opinion = get_ai_opinion(report, history)

    assert opinion is not None and opinion.verdict == Verdict.WAIT
    assert not opinion.used_search
    assert len(calls) == 2
    assert calls[0]["tools"] == [{"google_search": {}}]


def test_get_ai_opinion_falls_back_to_lighter_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_USE_SEARCH", "false")
    urls = []

    def fake_post(url, headers, json, timeout):
        urls.append(url)
        if "flash-latest" in url:
            return _Response(status=503)
        return _Response(_payload(ai_advisor.json.dumps(REPLY)))

    monkeypatch.setattr(ai_advisor.requests, "post", fake_post)
    report, history = _report()

    opinion = get_ai_opinion(report, history)

    assert opinion is not None and opinion.model == "gemini-flash-lite-latest"
    assert len(urls) == ai_advisor.ATTEMPTS_PER_MODEL + 1


def test_get_ai_opinion_returns_none_on_total_failure(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs["json"])
        return _Response(status=500)

    monkeypatch.setattr(ai_advisor.requests, "post", fake_post)
    report, history = _report()

    assert get_ai_opinion(report, history) is None
    assert len(calls) == ai_advisor.ATTEMPTS_PER_MODEL * (1 + len(ai_advisor.FALLBACK_MODELS))


def test_email_includes_ai_opinion():
    report, history = _report()
    opinion = parse_gemini_response(_payload(json.dumps(REPLY)), "gemini-x", used_search=False)

    assert "(AI: WAIT)" in build_email_subject(report, opinion)
    assert "AI second opinion: WAIT (medium confidence)" in build_email_body(report, opinion)
    html_body = build_email_html(report, opinion)
    assert "AI second opinion" in html_body
    assert "Copom meets next week" in html_body
    assert "Signals" in html_body


OUTLOOK_REPLY = dict(
    REPLY,
    outlook={
        "direction": "UP",
        "confidence": "low",
        "reasoning": "Peers weakened and Copom may cut.",
        "key_events": ["Oct 28 — Copom decision"],
    },
)


def test_parse_outlook():
    opinion = parse_gemini_response(_payload(json.dumps(OUTLOOK_REPLY)), "gemini-x", used_search=False)

    assert opinion.outlook is not None
    assert opinion.outlook.direction == "up"
    assert opinion.outlook.confidence == "low"
    assert opinion.outlook.key_events == ["Oct 28 — Copom decision"]


def test_parse_outlook_invalid_direction_is_dropped():
    reply = dict(REPLY, outlook={"direction": "moon", "reasoning": "?"})
    opinion = parse_gemini_response(_payload(json.dumps(reply)), "gemini-x", used_search=False)
    assert opinion.outlook is None


def test_build_prompt_includes_market_context():
    report, history = _report()
    context = {"selic_target_pct": 13.75}

    data = json.loads(build_prompt(report, history, context).split("\n", 1)[1])

    assert data["market_context"] == context


def test_email_includes_outlook():
    report, _ = _report()
    opinion = parse_gemini_response(_payload(json.dumps(OUTLOOK_REPLY)), "gemini-x", used_search=False)

    body = build_email_body(report, opinion)

    assert "Next 5 trading days: Dollar may rise" in body
    assert "Oct 28 — Copom decision" in body
    assert "Next 5 trading days" in build_email_html(report, opinion)


def test_plain_call_warns_model_search_is_unavailable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_USE_SEARCH", "false")
    bodies = []

    def fake_post(url, headers, json, timeout):
        bodies.append(json)
        return _Response(_payload(ai_advisor.json.dumps(REPLY)))

    monkeypatch.setattr(ai_advisor.requests, "post", fake_post)
    report, history = _report()

    get_ai_opinion(report, history)

    assert "tools" not in bodies[0]
    assert "Web search is unavailable" in bodies[0]["contents"][0]["parts"][0]["text"]
