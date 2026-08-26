"""Tests for percentile calculation and recommendation logic."""

from datetime import date

from src.analysis import (
    Verdict,
    build_report,
    compute_month_stats,
    format_percentile,
    percentile_rank,
)
from src.ptax import DailyRate


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

    report = build_report(month, rolling, reference=ref)

    assert report.today_rate == 5.50
    assert report.verdict == Verdict.EXCHANGE_TODAY


def test_build_report_wait_low_percentile():
    rolling = [DailyRate(date(2026, 7, d), 5.50) for d in range(1, 32)]
    rolling.extend(
        [DailyRate(date(2026, 8, d), 5.10 + d * 0.01) for d in range(1, 11)]
    )
    month = [DailyRate(date(2026, 8, d), 5.10 + d * 0.01) for d in range(1, 11)]
    ref = date(2026, 8, 3)

    report = build_report(month, rolling, reference=ref)

    assert report.verdict == Verdict.WAIT


def test_build_report_early_month_lean_exchange():
    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]
    rolling.append(DailyRate(date(2026, 8, 5), 5.45))

    month = _rates([(1, 5.40), (2, 5.42), (3, 5.44), (4, 5.43), (5, 5.45)])
    ref = date(2026, 8, 5)

    report = build_report(month, rolling, reference=ref, early_month_threshold=75)

    assert report.verdict == Verdict.EXCHANGE_TODAY
    assert report.early_month_note is not None


def test_format_percentile():
    assert format_percentile(82.3) == "82nd"
    assert format_percentile(None) == "N/A"


def test_build_email_body_contains_key_sections():
    from src.email_builder import build_email_body

    rolling = [DailyRate(date(2026, 7, d), 5.30) for d in range(1, 32)]
    rolling.append(DailyRate(date(2026, 8, 8), 5.45))
    month = [DailyRate(date(2026, 8, d), 5.35 + d * 0.01) for d in range(1, 9)]
    ref = date(2026, 8, 8)

    report = build_report(month, rolling, reference=ref)
    body = build_email_body(report)

    assert "USD/BRL Exchange Report" in body
    assert "RECOMMENDATION:" in body
    assert "PTAX is a reference rate" in body
