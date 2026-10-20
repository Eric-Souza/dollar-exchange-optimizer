"""Percentile-based exchange recommendation engine."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from src.ptax import DailyRate, TodayQuote


class Verdict(str, Enum):
    EXCHANGE_TODAY = "EXCHANGE TODAY"
    GOOD_DAY = "GOOD DAY"
    NEUTRAL = "NEUTRAL"
    WAIT = "WAIT"
    NO_QUOTE = "NO QUOTE"


# 5-day vs 20-day moving average gap needed to call a trend.
TREND_THRESHOLD_PCT = 0.3


@dataclass(frozen=True)
class MonthStats:
    best_date: date
    best_rate: float
    worst_date: date
    worst_rate: float
    average: float
    trading_days: int


@dataclass(frozen=True)
class Signals:
    """Trend, momentum and volatility context around today's rate."""

    quarter_percentile: float | None
    zscore_30d: float | None
    ma5: float | None
    ma20: float | None
    trend: str | None  # "up", "down" or "flat"
    change_1d_pct: float | None
    change_5d_pct: float | None
    volatility_20d_pct: float | None


@dataclass(frozen=True)
class ExchangeReport:
    reference_date: date
    today_rate: float | None
    rate_source: str | None
    rate_is_final: bool
    rolling_30d_percentile: float | None
    month_percentile: float | None
    month_stats: MonthStats | None
    signals: Signals | None
    verdict: Verdict
    explanation: str
    early_month_note: str | None
    end_of_month_note: str | None


def percentile_rank(value: float, values: list[float]) -> float:
    """Return percentile rank (0-100): share of values strictly below value."""
    if not values:
        return 0.0
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return ((below + 0.5 * equal) / len(values)) * 100


def compute_month_stats(rates: list[DailyRate]) -> MonthStats | None:
    if not rates:
        return None

    best = max(rates, key=lambda r: r.rate)
    worst = min(rates, key=lambda r: r.rate)
    average = sum(r.rate for r in rates) / len(rates)

    return MonthStats(
        best_date=best.date,
        best_rate=best.rate,
        worst_date=worst.date,
        worst_rate=worst.rate,
        average=average,
        trading_days=len(rates),
    )


def _pct_change(new: float, old: float) -> float:
    return ((new - old) / old) * 100


def compute_signals(
    past: list[DailyRate], today_rate: float, reference: date
) -> Signals:
    """Compute signals from past closings (before reference) plus today's rate."""
    series = [r.rate for r in past] + [today_rate]

    quarter = [r.rate for r in past if r.date >= reference - timedelta(days=90)]
    quarter_pct = (
        percentile_rank(today_rate, quarter + [today_rate]) if len(quarter) >= 20 else None
    )

    window_30d = [r.rate for r in past if r.date >= reference - timedelta(days=30)]
    zscore = None
    if len(window_30d) >= 5:
        stdev = statistics.pstdev(window_30d)
        if stdev > 0:
            zscore = (today_rate - statistics.fmean(window_30d)) / stdev

    ma5 = statistics.fmean(series[-5:]) if len(series) >= 5 else None
    ma20 = statistics.fmean(series[-20:]) if len(series) >= 20 else None
    trend = None
    if ma5 is not None and ma20 is not None:
        gap = _pct_change(ma5, ma20)
        trend = "up" if gap > TREND_THRESHOLD_PCT else "down" if gap < -TREND_THRESHOLD_PCT else "flat"

    returns = [_pct_change(b, a) for a, b in zip(series, series[1:])][-20:]
    volatility = statistics.pstdev(returns) if len(returns) >= 10 else None

    return Signals(
        quarter_percentile=quarter_pct,
        zscore_30d=zscore,
        ma5=ma5,
        ma20=ma20,
        trend=trend,
        change_1d_pct=_pct_change(today_rate, series[-2]) if len(series) >= 2 else None,
        change_5d_pct=_pct_change(today_rate, series[-6]) if len(series) >= 6 else None,
        volatility_20d_pct=volatility,
    )


def _determine_verdict(
    rolling_pct: float | None,
    month_pct: float | None,
    day_of_month: int,
    early_month_threshold: float,
) -> tuple[Verdict, str]:
    """Apply hybrid percentile tiers from the plan."""
    if rolling_pct is None:
        return (
            Verdict.NO_QUOTE,
            "No USD/BRL quote is available for today yet (holiday, or before "
            "BACEN's first bulletin around 10:00 BRT). No recommendation today.",
        )

    if day_of_month <= 10 and rolling_pct >= early_month_threshold:
        if month_pct is None or month_pct >= 65:
            return (
                Verdict.EXCHANGE_TODAY,
                "Rate is strong versus the last 30 days. With salary typically "
                "arriving early in the month, this is a favorable window to exchange.",
            )

    if rolling_pct >= 85 and (month_pct is None or month_pct >= 80):
        return (
            Verdict.EXCHANGE_TODAY,
            "Rate is in the top tier versus both the last 30 days and this month.",
        )

    if rolling_pct >= 70 and (month_pct is None or month_pct >= 65):
        return (
            Verdict.GOOD_DAY,
            "Rate is above average — reasonable to exchange if you need BRL now.",
        )

    if rolling_pct >= 50:
        return (
            Verdict.NEUTRAL,
            "Rate is acceptable but not optimal. Waiting may yield a better rate.",
        )

    if month_pct is not None and month_pct < 40:
        return (
            Verdict.WAIT,
            "Rate is below median for both windows — historically, better days may still come.",
        )

    return (
        Verdict.WAIT,
        "Rate is below the 30-day median — waiting may improve your exchange rate.",
    )


def _apply_signals(
    verdict: Verdict, explanation: str, signals: Signals | None
) -> tuple[Verdict, str]:
    """Refine the percentile verdict with longer-window and trend context."""
    if signals is None:
        return verdict, explanation

    if (
        verdict == Verdict.NEUTRAL
        and signals.quarter_percentile is not None
        and signals.quarter_percentile >= 80
    ):
        verdict = Verdict.GOOD_DAY
        explanation = (
            "Rate is middling for the last 30 days but in the top 20% of the "
            "last 90 days — the whole recent stretch has been unusually strong."
        )

    if signals.trend == "down" and verdict in (Verdict.GOOD_DAY, Verdict.NEUTRAL):
        explanation += (
            " The dollar has been sliding (5-day average below 20-day), so "
            "waiting carries the risk of a lower rate."
        )
    elif signals.trend == "up" and verdict in (Verdict.NEUTRAL, Verdict.WAIT):
        explanation += (
            " The dollar has been climbing (5-day average above 20-day), which "
            "supports waiting a few more days."
        )

    return verdict, explanation


def _early_month_note(day_of_month: int) -> str | None:
    if day_of_month <= 10:
        return (
            "Primary exchange window: you typically receive USD at the start of "
            "the month (days 1–10)."
        )
    return None


def remaining_weekdays(reference: date) -> int:
    """Weekdays left in the month after the reference date."""
    remaining = 0
    d = reference + timedelta(days=1)
    while d.month == reference.month:
        if d.weekday() < 5:
            remaining += 1
        d += timedelta(days=1)
    return remaining


def _end_of_month_note(
    reference: date,
    month_rates: list[DailyRate],
    month_pct: float | None,
    today_rate: float | None,
) -> str | None:
    if reference.day < 25 or not month_rates or today_rate is None:
        return None

    if month_pct is not None and month_pct >= 60:
        return None

    if remaining_weekdays(reference) >= 5:
        return None

    best = max(month_rates, key=lambda r: r.rate)
    if best.rate <= 0:
        return None

    pct_below = ((best.rate - today_rate) / best.rate) * 100
    return (
        f"Best day this month was {best.date.strftime('%b %d')} at R$ {best.rate:.4f}; "
        f"today is {pct_below:.1f}% below that — consider exchanging soon if you haven't yet."
    )


def build_report(
    history: list[DailyRate],
    reference: date | None = None,
    today_quote: TodayQuote | None = None,
    early_month_threshold: float = 75.0,
) -> ExchangeReport:
    """Build a full exchange recommendation report.

    history holds PTAX closing rates; today_quote is the best available quote
    for the reference date. Without one, today's closing in history is used.
    """
    ref = reference or date.today()
    history = sorted((r for r in history if r.date <= ref), key=lambda r: r.date)
    past = [r for r in history if r.date < ref]

    if today_quote is None:
        closing = next((r for r in history if r.date == ref), None)
        if closing is not None:
            today_quote = TodayQuote(closing.rate, "PTAX compra (closing)", is_final=True)
    today_rate = today_quote.rate if today_quote else None

    month_start = ref.replace(day=1)
    rolling_start = ref - timedelta(days=30)
    month_past = [r for r in past if r.date >= month_start]
    rolling_values = [r.rate for r in past if r.date >= rolling_start]
    month_values = [r.rate for r in month_past]

    # Month stats include today's quote so best/worst reflect it.
    month_rates = month_past + ([DailyRate(ref, today_rate)] if today_rate is not None else [])
    month_stats = compute_month_stats(month_rates)

    rolling_pct = None
    month_pct = None
    signals = None
    if today_rate is not None:
        rolling_pct = percentile_rank(today_rate, rolling_values + [today_rate])
        if len(month_values) + 1 >= 5:
            month_pct = percentile_rank(today_rate, month_values + [today_rate])
        signals = compute_signals(past, today_rate, ref)

    verdict, explanation = _determine_verdict(
        rolling_pct, month_pct, ref.day, early_month_threshold
    )
    verdict, explanation = _apply_signals(verdict, explanation, signals)

    return ExchangeReport(
        reference_date=ref,
        today_rate=today_rate,
        rate_source=today_quote.source if today_quote else None,
        rate_is_final=today_quote.is_final if today_quote else False,
        rolling_30d_percentile=rolling_pct,
        month_percentile=month_pct,
        month_stats=month_stats,
        signals=signals,
        verdict=verdict,
        explanation=explanation,
        early_month_note=_early_month_note(ref.day),
        end_of_month_note=_end_of_month_note(ref, month_rates, month_pct, today_rate),
    )


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_percentile(value: float | None) -> str:
    if value is None:
        return "N/A"
    return _ordinal(round(value))
