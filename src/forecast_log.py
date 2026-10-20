"""Daily log of AI outlooks, scored against what the rate actually did."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.ptax import DailyRate

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "forecasts.json")
HORIZON_TRADING_DAYS = 5
# Moves smaller than this over the horizon count as "sideways".
SIDEWAYS_PCT = 0.5
MAX_ENTRIES = 500


@dataclass(frozen=True)
class ScoredForecast:
    date: date
    direction: str
    confidence: str | None
    start_rate: float
    end_date: date
    end_rate: float
    change_pct: float
    actual: str

    @property
    def hit(self) -> bool:
        return self.direction == self.actual


@dataclass(frozen=True)
class TrackRecord:
    scored: list[ScoredForecast]  # newest first
    pending: int

    @property
    def hits(self) -> int:
        return sum(1 for s in self.scored if s.hit)

    @property
    def hit_rate(self) -> float | None:
        return self.hits / len(self.scored) * 100 if self.scored else None


def load_log(path: str = LOG_PATH) -> list[dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_log(entries: list[dict[str, Any]], path: str = LOG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
        f.write("\n")


def record(entries: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Add today's entry, replacing any earlier run on the same date."""
    kept = [e for e in entries if e["date"] != entry["date"]]
    return sorted(kept + [entry], key=lambda e: e["date"])[-MAX_ENTRIES:]


def classify_move(change_pct: float) -> str:
    if change_pct > SIDEWAYS_PCT:
        return "up"
    if change_pct < -SIDEWAYS_PCT:
        return "down"
    return "sideways"


def score(entries: list[dict[str, Any]], history: list[DailyRate]) -> TrackRecord:
    """Score each logged outlook against the closing rate N trading days later."""
    closings = sorted(history, key=lambda r: r.date)
    by_date = {r.date: r.rate for r in closings}
    scored: list[ScoredForecast] = []
    pending = 0

    for entry in entries:
        direction = entry.get("direction")
        if not direction:
            continue
        day = date.fromisoformat(entry["date"])
        if not closings or day < closings[0].date:
            continue  # older than the fetched history; can't be scored
        later = [r for r in closings if r.date > day]
        if len(later) < HORIZON_TRADING_DAYS:
            pending += 1
            continue
        end = later[HORIZON_TRADING_DAYS - 1]
        # Prefer that day's closing over the intraday quote the email used.
        start_rate = by_date.get(day, entry["rate"])
        change = (end.rate - start_rate) / start_rate * 100
        scored.append(
            ScoredForecast(
                date=day,
                direction=direction,
                confidence=entry.get("confidence"),
                start_rate=start_rate,
                end_date=end.date,
                end_rate=end.rate,
                change_pct=change,
                actual=classify_move(change),
            )
        )

    scored.sort(key=lambda s: s.date, reverse=True)
    return TrackRecord(scored=scored, pending=pending)
