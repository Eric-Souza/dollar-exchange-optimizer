"""Tests for PTAX response parsing."""

from datetime import date

from src.ptax import CLOSING_BULLETIN, DailyRate, parse_ptax_response


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
