"""Tests for PTAX response parsing."""

from datetime import date

from src.ptax import (
    CLOSING_BULLETIN,
    DailyRate,
    TodayQuote,
    closing_rates,
    latest_ptax_quote,
    parse_awesomeapi_response,
    parse_bulletins,
    parse_ptax_response,
)


def test_parse_ptax_response_closing_only():
    entries = [
        {
            "cotacaoCompra": 5.10,
            "cotacaoVenda": 5.11,
            "dataHoraCotacao": "2026-08-01 10:00:00.0",
            "tipoBoletim": "Abertura",
        },
        {
            "cotacaoCompra": 5.12,
            "cotacaoVenda": 5.13,
            "dataHoraCotacao": "2026-08-01 13:00:00.0",
            "tipoBoletim": CLOSING_BULLETIN,
        },
        {
            "cotacaoCompra": 5.20,
            "cotacaoVenda": 5.21,
            "dataHoraCotacao": "2026-08-02 13:00:00.0",
            "tipoBoletim": CLOSING_BULLETIN,
        },
    ]

    rates = parse_ptax_response(entries)

    assert rates == [
        DailyRate(date(2026, 8, 1), 5.12),
        DailyRate(date(2026, 8, 2), 5.20),
    ]


def test_parse_ptax_response_empty():
    assert parse_ptax_response([]) == []


def test_parse_ptax_response_ignores_non_closing():
    entries = [
        {
            "cotacaoCompra": 5.10,
            "cotacaoVenda": 5.11,
            "dataHoraCotacao": "2026-08-01 10:00:00.0",
            "tipoBoletim": "Intermediário",
        },
    ]
    assert parse_ptax_response(entries) == []


def test_parse_ptax_response_without_bulletin_type_is_closing():
    entries = [{"cotacaoCompra": 5.04, "dataHoraCotacao": "2026-10-14 13:05:17.833426"}]
    assert parse_ptax_response(entries) == [DailyRate(date(2026, 10, 14), 5.04)]


def test_parse_ptax_response_accepts_legacy_closing_label():
    entries = [
        {
            "cotacaoCompra": 5.04,
            "dataHoraCotacao": "2026-10-14 13:05:17",
            "tipoBoletim": "Fechamento PTAX",
        }
    ]
    assert parse_ptax_response(entries) == [DailyRate(date(2026, 10, 14), 5.04)]


def _bulletins():
    return parse_bulletins(
        [
            {"cotacaoCompra": 5.08, "dataHoraCotacao": "2026-10-19 13:05:47.2", "tipoBoletim": "Fechamento"},
            {"cotacaoCompra": 5.02, "dataHoraCotacao": "2026-10-20 10:05:10.0", "tipoBoletim": "Abertura"},
            {"cotacaoCompra": 5.01, "dataHoraCotacao": "2026-10-20 11:09:09.3", "tipoBoletim": "Intermediário"},
        ]
    )


def test_latest_ptax_quote_intraday():
    quote = latest_ptax_quote(_bulletins(), date(2026, 10, 20))
    assert quote == TodayQuote(5.01, "PTAX compra (intermediário 11:09)", is_final=False)


def test_latest_ptax_quote_closing_and_missing():
    quote = latest_ptax_quote(_bulletins(), date(2026, 10, 19))
    assert quote == TodayQuote(5.08, "PTAX compra (closing)", is_final=True)
    assert latest_ptax_quote(_bulletins(), date(2026, 10, 21)) is None


def test_closing_rates_skip_intraday():
    assert closing_rates(_bulletins()) == [DailyRate(date(2026, 10, 19), 5.08)]


def test_parse_awesomeapi_response():
    payload = {"USDBRL": {"bid": "5.0057", "ask": "5.008", "create_date": "2026-10-20 13:09:23"}}

    quote = parse_awesomeapi_response(payload, date(2026, 10, 20))

    assert quote == TodayQuote(5.0057, "Market bid 13:09 (AwesomeAPI)", is_final=False)
    assert parse_awesomeapi_response(payload, date(2026, 10, 21)) is None
