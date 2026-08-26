"""BACEN PTAX API client for USD/BRL exchange rates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests

PTAX_BASE_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
)
CLOSING_BULLETIN = "Fechamento PTAX"


@dataclass(frozen=True)
class DailyRate:
    """A single day's PTAX closing buy rate."""

    date: date
    rate: float


def _format_bcb_date(d: date) -> str:
    return d.strftime("%m-%d-%Y")


def _parse_cotacao_date(value: str) -> date:
    """Parse BACEN dataHoraCotacao (e.g. '2026-08-26 13:00:00.0')."""
    return datetime.strptime(value.split()[0], "%Y-%m-%d").date()


def parse_ptax_response(entries: list[dict[str, Any]]) -> list[DailyRate]:
    """Extract closing PTAX buy rates, one per business day."""
    by_date: dict[date, float] = {}

    for entry in entries:
        bulletin = entry.get("tipoBoletim")
        if bulletin is not None and bulletin != CLOSING_BULLETIN:
            continue

        cotacao_date = _parse_cotacao_date(entry["dataHoraCotacao"])
        by_date[cotacao_date] = float(entry["cotacaoCompra"])

    return [DailyRate(d, rate) for d, rate in sorted(by_date.items())]


def fetch_rates(start: date, end: date) -> list[DailyRate]:
    """Fetch PTAX closing buy rates for a date range (inclusive)."""
    params = {
        "@dataInicial": f"'{_format_bcb_date(start)}'",
        "@dataFinalCotacao": f"'{_format_bcb_date(end)}'",
        "$format": "json",
    }
    response = requests.get(PTAX_BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    entries = response.json().get("value", [])
    return parse_ptax_response(entries)


def fetch_month_rates(reference: date | None = None) -> list[DailyRate]:
    """Fetch all PTAX rates for the month containing reference date."""
    ref = reference or date.today()
    start = ref.replace(day=1)
    return fetch_rates(start, ref)


def fetch_rolling_rates(days: int = 30, reference: date | None = None) -> list[DailyRate]:
    """Fetch PTAX rates for the last N calendar days ending on reference date."""
    ref = reference or date.today()
    start = ref - timedelta(days=days)
    return fetch_rates(start, ref)
