"""Tests for headlines, the forecast log and the richer email sections."""

from datetime import date, timedelta

from src.analysis import build_report
from src.email_builder import EmailExtras, build_email_body, build_email_html
from src.forecast_log import TrackRecord, classify_move, load_log, record, save_log, score
from src.news import parse_rss
from src.ptax import DailyRate

RSS = """<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Dólar fecha em alta com Fed - Valor Econômico</title>
    <link>https://news.example/1</link>
    <pubDate>Tue, 20 Oct 2026 14:00:00 GMT</pubDate>
    <source url="https://valor.globo.com">Valor Econômico</source>
  </item>
  <item>
    <title>Real recua</title>
    <link>https://news.example/2</link>
    <pubDate>not a date</pubDate>
  </item>
</channel></rss>"""


def _weekdays(start: date, count: int, rate: float = 5.0, step: float = 0.0) -> list[DailyRate]:
    rates, d = [], start
    while len(rates) < count:
        if d.weekday() < 5:
            rates.append(DailyRate(d, round(rate + step * len(rates), 4)))
        d += timedelta(days=1)
    return rates


def test_parse_rss_strips_source_suffix():
    headlines = parse_rss(RSS)

    assert headlines[0].title == "Dólar fecha em alta com Fed"
    assert headlines[0].source == "Valor Econômico"
    assert headlines[0].published.year == 2026
    assert headlines[1].published is None
    assert len(parse_rss(RSS, limit=1)) == 1


def test_classify_move():
    assert classify_move(0.8) == "up"
    assert classify_move(-0.6) == "down"
    assert classify_move(0.3) == "sideways"


def test_record_replaces_same_day():
    entries = record([{"date": "2026-10-19"}], {"date": "2026-10-20", "v": 1})
    entries = record(entries, {"date": "2026-10-20", "v": 2})
    assert entries == [{"date": "2026-10-19"}, {"date": "2026-10-20", "v": 2}]


def test_save_and_load_log(tmp_path):
    path = str(tmp_path / "data" / "log.json")
    assert load_log(path) == []
    save_log([{"date": "2026-10-20"}], path)
    assert load_log(path) == [{"date": "2026-10-20"}]


def test_score_uses_fifth_trading_day_closing():
    history = _weekdays(date(2026, 10, 1), 12, rate=5.0, step=0.01)
    entries = [
        {"date": "2026-10-01", "rate": 4.9, "direction": "up", "confidence": "low"},
        {"date": "2026-10-14", "rate": 5.1, "direction": "down"},  # too recent
        {"date": "2026-10-02", "rate": 5.0, "direction": None},  # no AI outlook
        {"date": "2026-09-01", "rate": 5.0, "direction": "up"},  # before history
    ]

    track = score(entries, history)

    assert track.pending == 1
    assert len(track.scored) == 1
    scored = track.scored[0]
    assert scored.start_rate == 5.0  # that day's closing, not the logged quote
    assert scored.end_date == history[5].date
    assert scored.actual == "up" and scored.hit
    assert track.hit_rate == 100


def _rich_report():
    history = _weekdays(date(2026, 9, 1), 36, rate=5.30, step=0.002)
    reference = history[-1].date
    return build_report(history, reference=reference), history


def test_email_shows_extras():
    report, history = _rich_report()
    extras = EmailExtras(
        history=history,
        market={
            "focus_survey_usdbrl_by_month": [{"month": "10/2026", "median": 5.30}],
            "selic_target_pct": 13.75,
            "last_copom": {"date": "2026-09-16T00:00:00", "decision": "Copom reduz a Selic"},
            "peer_currencies": {"USD-MXN": {"label": "peso", "change_1d_pct": 0.4}},
        },
        headlines=parse_rss(RSS),
        track_record=TrackRecord(scored=[], pending=3),
        salary_usd=5000,
    )

    body = build_email_body(report, None, extras)
    page = build_email_html(report, None, extras)

    assert "US$ 5,000 today → R$" in body
    assert "Focus 10/2026" in body and "Selic target: 13.75%" in body
    assert "Last 30 trading days" in body
    assert "Dólar fecha em alta com Fed (Valor Econômico)" in body
    for text in ("Your salary", "Last 30 trading days", "Market context", "USD/MXN",
                 "Headlines", "https://news.example/1", "Weekdays left"):
        assert text in page
    assert page.count('vertical-align:bottom;padding:0 1px;') == 30


def test_email_without_extras_still_renders():
    report, _ = _rich_report()
    assert "Your salary" not in build_email_html(report)
    assert "Market context" not in build_email_body(report)
