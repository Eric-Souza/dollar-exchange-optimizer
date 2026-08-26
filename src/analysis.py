"""Percentile-based exchange recommendation engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from src.ptax import DailyRate


class Verdict(str, Enum):
    EXCHANGE_TODAY = "EXCHANGE TODAY"
    GOOD_DAY = "GOOD DAY"
    NEUTRAL = "NEUTRAL"
    WAIT = "WAIT"


@dataclass(frozen=True)
class MonthStats:
    best_date: date
    best_rate: float
    worst_date: date
    worst_rate: float
    average: float
    trading_days: int


@dataclass(frozen=True)
class ExchangeReport:
    reference_date: date
    today_rate: float | None
    rolling_30d_percentile: float | None
    month_percentile: float | None
    month_stats: MonthStats | None
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


def _determine_verdict(
    rolling_pct: float | None,
    month_pct: float | None,
    day_of_month: int,
    early_month_threshold: float,
) -> tuple[Verdict, str]:
    """Apply hybrid percentile tiers from the plan."""
    if rolling_pct is None:
        return Verdict.NEUTRAL, "Insufficient rate history to compute a percentile."

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


def _early_month_note(day_of_month: int) -> str | None:
    if day_of_month <= 10:
        return (
            "Primary exchange window: you typically receive USD at the start of "
            "the month (days 1–10)."
        )
    return None


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

    remaining = 0
    d = reference + timedelta(days=1)
    while d.month == reference.month:
        if d.weekday() < 5:
            remaining += 1
        d += timedelta(days=1)

    if remaining >= 5:
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
    month_rates: list[DailyRate],
    rolling_rates: list[DailyRate],
    reference: date | None = None,
    early_month_threshold: float = 75.0,
) -> ExchangeReport:
    """Build a full exchange recommendation report."""
    ref = reference or date.today()
    month_rates = [r for r in month_rates if r.date <= ref]
    rolling_rates = [r for r in rolling_rates if r.date <= ref]
    month_stats = compute_month_stats(month_rates)

    today_rate: float | None = None
    for rates in (month_rates, rolling_rates):
        for r in rates:
            if r.date == ref:
                today_rate = r.rate
                break
        if today_rate is not None:
            break

    rolling_values = [r.rate for r in rolling_rates]
    month_values = [r.rate for r in month_rates]

    rolling_pct = (
        percentile_rank(today_rate, rolling_values) if today_rate is not None else None
    )
    month_pct = None
    if today_rate is not None and len(month_values) >= 5:
        month_pct = percentile_rank(today_rate, month_values)

    verdict, explanation = _determine_verdict(
        rolling_pct, month_pct, ref.day, early_month_threshold
    )

    return ExchangeReport(
        reference_date=ref,
        today_rate=today_rate,
        rolling_30d_percentile=rolling_pct,
        month_percentile=month_pct,
        month_stats=month_stats,
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
