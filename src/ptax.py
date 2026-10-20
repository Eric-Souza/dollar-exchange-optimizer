"""BACEN PTAX API client for USD/BRL exchange rates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests

PTAX_BASE_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
)
AWESOMEAPI_URL = "https://economia.awesomeapi.com.br/json/last/USD-BRL"

# BACEN labels the closing bulletin "Fechamento" (older docs: "Fechamento PTAX").
CLOSING_BULLETIN = "Fechamento"


@dataclass(frozen=True)
class DailyRate:
    """A single day's PTAX closing buy rate."""

    date: date
    rate: float


@dataclass(frozen=True)
class Bulletin:
    """A single PTAX bulletin (opening, intermediate, or closing)."""

    timestamp: datetime
    kind: str
    rate: float

    @property
    def is_closing(self) -> bool:
        return self.kind.startswith(CLOSING_BULLETIN)


@dataclass(frozen=True)
class TodayQuote:
    """The best available quote for the reference date."""

    rate: float
    source: str
    is_final: bool


def _format_bcb_date(d: date) -> str:
    return d.strftime("%m-%d-%Y")


def _parse_cotacao_timestamp(value: str) -> datetime:
    """Parse BACEN dataHoraCotacao (e.g. '2026-08-26 13:04:14.548156')."""
    return datetime.strptime(value.split(".")[0], "%Y-%m-%d %H:%M:%S")


def parse_bulletins(entries: list[dict[str, Any]]) -> list[Bulletin]:
    """Parse raw PTAX entries into bulletins sorted by time."""
    bulletins = [
        Bulletin(
            timestamp=_parse_cotacao_timestamp(entry["dataHoraCotacao"]),
            # Endpoints without tipoBoletim only return closing rates.
            kind=entry.get("tipoBoletim") or CLOSING_BULLETIN,
            rate=float(entry["cotacaoCompra"]),
        )
        for entry in entries
    ]
    return sorted(bulletins, key=lambda b: b.timestamp)


def closing_rates(bulletins: list[Bulletin]) -> list[DailyRate]:
    """Extract closing PTAX buy rates, one per business day."""
    by_date = {b.timestamp.date(): b.rate for b in bulletins if b.is_closing}
    return [DailyRate(d, rate) for d, rate in sorted(by_date.items())]


def parse_ptax_response(entries: list[dict[str, Any]]) -> list[DailyRate]:
    """Extract closing PTAX buy rates, one per business day."""
    return closing_rates(parse_bulletins(entries))


def fetch_bulletins(start: date, end: date) -> list[Bulletin]:
    """Fetch all USD PTAX bulletins for a date range (inclusive)."""
    params = {
        "@moeda": "'USD'",
        "@dataInicial": f"'{_format_bcb_date(start)}'",
        "@dataFinalCotacao": f"'{_format_bcb_date(end)}'",
        "$format": "json",
    }
    response = requests.get(PTAX_BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    return parse_bulletins(response.json().get("value", []))


def fetch_rates(start: date, end: date) -> list[DailyRate]:
    """Fetch PTAX closing buy rates for a date range (inclusive)."""
    return closing_rates(fetch_bulletins(start, end))


def latest_ptax_quote(bulletins: list[Bulletin], reference: date) -> TodayQuote | None:
    """Return the most recent PTAX bulletin published on the reference date."""
    todays = [b for b in bulletins if b.timestamp.date() == reference]
    if not todays:
        return None
    latest = todays[-1]
    if latest.is_closing:
        return TodayQuote(latest.rate, "PTAX compra (closing)", is_final=True)
    return TodayQuote(
        latest.rate,
        f"PTAX compra ({latest.kind.lower()} {latest.timestamp.strftime('%H:%M')})",
        is_final=False,
    )


def parse_awesomeapi_response(payload: dict[str, Any], reference: date) -> TodayQuote | None:
    """Parse AwesomeAPI's USD-BRL quote, ignoring quotes from other days."""
    quote = payload.get("USDBRL")
    if not quote:
        return None
    created = datetime.strptime(quote["create_date"], "%Y-%m-%d %H:%M:%S")
    if created.date() != reference:
        return None
    return TodayQuote(
        float(quote["bid"]),
        f"Market bid {created.strftime('%H:%M')} (AwesomeAPI)",
        is_final=False,
    )


def fetch_live_quote(reference: date) -> TodayQuote | None:
    """Fetch a live market quote; returns None on any failure."""
    try:
        response = requests.get(AWESOMEAPI_URL, timeout=15)
        response.raise_for_status()
        return parse_awesomeapi_response(response.json(), reference)
    except (requests.RequestException, ValueError, KeyError):
        return None


def fetch_history(
    reference: date, days: int = 120
) -> tuple[list[DailyRate], TodayQuote | None]:
    """Fetch closing history plus the best available quote for the reference date.

    Prefers today's latest PTAX bulletin; falls back to a live market quote
    when BACEN hasn't published anything yet (before ~10:00 BRT or holidays).
    """
    bulletins = fetch_bulletins(reference - timedelta(days=days), reference)
    today = latest_ptax_quote(bulletins, reference) or fetch_live_quote(reference)
    return closing_rates(bulletins), today
