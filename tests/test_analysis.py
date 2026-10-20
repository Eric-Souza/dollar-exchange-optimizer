"""Tests for percentile calculation and recommendation logic."""

from datetime import date, timedelta

from src.analysis import (
    Verdict,
    build_report,
    compute_month_stats,
    compute_signals,
    format_percentile,
    percentile_rank,
)
from src.ptax import DailyRate, TodayQuote


def _history(*groups: list[DailyRate]) -> list[DailyRate]:
    """Merge rate lists into one history, de-duplicated by date."""
    return list({r.date: r for group in groups for r in group}.values())


def _rates(values: list[tuple[int, float]], year: int = 2026, month: int = 8) -> list[DailyRate]:
    return [DailyRate(date(year, month, day), rate) for day, rate in values]


def test_percentile_rank_median():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile_rank(3.0, values) == 50.0


def test_percentile_rank_highest():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile_rank(5.0, values) == 90.0


def test_percentile_rank_lowest():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile_rank(1.0, values) == 10.0


def test_compute_month_stats():
    rates = _rates([(1, 5.0), (2, 5.5), (3, 4.8)])
    stats = compute_month_stats(rates)

    assert stats is not None
    assert stats.best_rate == 5.5
    assert stats.worst_rate == 4.8
    assert stats.trading_days == 3
    assert abs(stats.average - 5.1) < 0.001


def test_build_report_exchange_today_high_percentile():
    rolling = _rates([(d, 5.0 + d * 0.01) for d in range(1, 31)], month=7)
    rolling.append(DailyRate(date(2026, 8, 5), 5.50))

    month = _rates([(1, 5.10), (2, 5.15), (3, 5.20), (4, 5.25), (5, 5.50)])
    ref = date(2026, 8, 5)

    report = build_report(_history(rolling, month), reference=ref)

    assert report.today_rate == 5.50
    assert report.verdict == Verdict.EXCHANGE_TODAY


def test_build_report_wait_low_percentile():
    rolling = [DailyRate(date(2026, 7, d), 5.50) for d in range(1, 32)]
    rolling.extend(
        [DailyRate(date(2026, 8, d), 5.10 + d * 0.01) for d in range(1, 11)]
    )
    month = [DailyRate(date(2026, 8, d), 5.10 + d * 0.01) for d in range(1, 11)]
    ref = date(2026, 8, 3)

    report = build_report(_history(rolling, month), reference=ref)

    assert report.verdict == Verdict.WAIT


def test_build_report_early_month_lean_exchange():
    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]
    rolling.append(DailyRate(date(2026, 8, 5), 5.45))

    month = _rates([(1, 5.40), (2, 5.42), (3, 5.44), (4, 5.43), (5, 5.45)])
    ref = date(2026, 8, 5)

    report = build_report(_history(rolling, month), reference=ref, early_month_threshold=75)

    assert report.verdict == Verdict.EXCHANGE_TODAY
    assert report.early_month_note is not None


def test_format_percentile():
    assert format_percentile(82.3) == "82nd"
    assert format_percentile(None) == "N/A"


def test_build_email_body_contains_key_sections():
    from src.email_builder import build_email_body, build_email_html

    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]
    rolling.append(DailyRate(date(2026, 8, 8), 5.45))
    month = [DailyRate(date(2026, 8, d), 5.35 + d * 0.01) for d in range(1, 9)]
    ref = date(2026, 8, 8)

    report = build_report(_history(rolling, month), reference=ref)
    body = build_email_body(report)
    html_body = build_email_html(report)

    assert "USD/BRL Exchange Report" in body
    assert "RECOMMENDATION" not in body  # plain text uses verdict label first
    assert "Good day" in body or "EXCHANGE" in body or "Neutral" in body or "Wait" in body
    assert "PTAX is a reference rate" in body

    assert "Recommendation" in html_body
    assert "USD → BRL" in html_body
    assert "PTAX compra" in html_body
    assert "This month" in html_body


def test_build_report_no_quote_is_explicit():
    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]

    report = build_report(rolling, reference=date(2026, 8, 3))

    assert report.today_rate is None
    assert report.verdict == Verdict.NO_QUOTE
    assert report.signals is None


def test_build_report_uses_intraday_quote():
    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]
    quote = TodayQuote(5.45, "PTAX compra (intermediário 11:00)", is_final=False)

    report = build_report(rolling, reference=date(2026, 8, 3), today_quote=quote)

    assert report.today_rate == 5.45
    assert report.rate_source == "PTAX compra (intermediário 11:00)"
    assert not report.rate_is_final
    assert report.verdict == Verdict.EXCHANGE_TODAY


def test_compute_signals_trend_and_changes():
    ref = date(2026, 8, 31)
    past = [DailyRate(ref - timedelta(days=30 - i), 5.0 + i * 0.01) for i in range(30)]

    signals = compute_signals(past, 5.40, ref)

    assert signals.trend == "up"
    assert signals.change_1d_pct is not None and signals.change_1d_pct > 0
    assert signals.zscore_30d is not None and signals.zscore_30d > 1
    assert signals.volatility_20d_pct is not None


def test_compute_signals_short_history():
    ref = date(2026, 8, 5)
    past = [DailyRate(date(2026, 8, d), 5.0) for d in (1, 2, 3)]

    signals = compute_signals(past, 5.0, ref)

    assert signals.quarter_percentile is None
    assert signals.ma20 is None
    assert signals.trend is None
    assert signals.zscore_30d is None


def test_quarter_percentile_upgrades_neutral():
    # Last 30 days hover at a high plateau; the prior 60 days were much lower.
    ref = date(2026, 8, 20)
    low = [DailyRate(ref - timedelta(days=d), 5.00) for d in range(31, 91)]
    plateau = [
        DailyRate(ref - timedelta(days=d), 5.40 + (0.01 if d % 2 else -0.01))
        for d in range(1, 31)
    ]
    quote = TodayQuote(5.40, "PTAX compra (closing)", is_final=True)

    report = build_report(low + plateau, reference=ref, today_quote=quote)

    assert report.signals.quarter_percentile >= 80
    assert report.verdict == Verdict.GOOD_DAY
