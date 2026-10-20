"""Free market data that helps the AI judge where USD/BRL is heading.

Every source is optional: a failure just drops that piece of context.
"""

from __future__ import annotations

import sys
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

import requests

FOCUS_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
    "ExpectativaMercadoMensais"
)
SELIC_TARGET_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1"
AWESOMEAPI_DAILY_URL = "https://economia.awesomeapi.com.br/json/daily/{pair}/6"

# EUR-USD tracks the global dollar (inverse); the others are EM peers of the real.
PEER_PAIRS = {
    "EUR-USD": "EUR/USD — rising means a weaker global dollar",
    "USD-MXN": "Mexican peso (EM peer)",
    "USD-CLP": "Chilean peso (EM peer, copper-linked)",
    "USD-ZAR": "South African rand (EM peer, risk-sensitive)",
}


def _next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def parse_focus_response(entries: list[dict[str, Any]], months: list[str]) -> list[dict[str, Any]]:
    """Keep the latest survey's row for each requested month (MM/YYYY)."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in sorted(entries, key=lambda e: e["Data"], reverse=True):
        month = entry["DataReferencia"]
        if month in months and month not in latest:
            latest[month] = {
                "month": month,
                "median": entry["Mediana"],
                "min": entry["Minimo"],
                "max": entry["Maximo"],
                "respondents": entry["numeroRespondentes"],
                "survey_date": entry["Data"],
            }
    return [latest[m] for m in months if m in latest]


def fetch_focus_expectations(reference: date) -> list[dict[str, Any]]:
    """BACEN Focus survey: market median USD/BRL for this month and next."""
    months = [reference.strftime("%m/%Y"), _next_month(reference).strftime("%m/%Y")]
    month_filter = " or ".join(f"DataReferencia eq '{m}'" for m in months)
    params = {
        "$filter": f"Indicador eq 'Câmbio' and ({month_filter}) and baseCalculo eq 0",
        "$orderby": "Data desc",
        "$top": "10",
        "$format": "json",
    }
    # Olinda rejects '+' for spaces, so encode them as %20.
    response = requests.get(f"{FOCUS_URL}?{urlencode(params, quote_via=quote)}", timeout=30)
    response.raise_for_status()
    return parse_focus_response(response.json().get("value", []), months)


def fetch_selic_target() -> float:
    response = requests.get(SELIC_TARGET_URL, params={"formato": "json"}, timeout=30)
    response.raise_for_status()
    return float(response.json()[-1]["valor"])


def parse_daily_moves(entries: list[dict[str, Any]]) -> dict[str, float]:
    """1-day and 5-day % moves from AwesomeAPI daily quotes (newest first)."""
    bids = [float(e["bid"]) for e in entries]
    moves: dict[str, float] = {}
    if len(bids) >= 2:
        moves["change_1d_pct"] = round((bids[0] / bids[1] - 1) * 100, 2)
    if len(bids) >= 6:
        moves["change_5d_pct"] = round((bids[0] / bids[5] - 1) * 100, 2)
    return moves


def fetch_peer_moves() -> dict[str, dict[str, Any]]:
    peers: dict[str, dict[str, Any]] = {}
    for pair, label in PEER_PAIRS.items():
        try:
            response = requests.get(AWESOMEAPI_DAILY_URL.format(pair=pair), timeout=15)
            response.raise_for_status()
            peers[pair] = {"label": label, **parse_daily_moves(response.json())}
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"Peer quote {pair} unavailable: {exc}", file=sys.stderr)
    return peers


def fetch_market_context(reference: date) -> dict[str, Any]:
    """Collect whatever context is available; never raises."""
    context: dict[str, Any] = {}
    sources = {
        "focus_survey_usdbrl_by_month": lambda: fetch_focus_expectations(reference),
        "selic_target_pct": fetch_selic_target,
        "peer_currencies": fetch_peer_moves,
    }
    for key, fetch in sources.items():
        try:
            value = fetch()
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            print(f"Market context {key} unavailable: {exc}", file=sys.stderr)
            continue
        if value:
            context[key] = value
    return context
