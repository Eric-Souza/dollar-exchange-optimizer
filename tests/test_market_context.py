"""Tests for market context parsing."""

from src.market_context import parse_daily_moves, parse_focus_response


def test_parse_focus_response_keeps_latest_survey_per_month():
    entries = [
        {"Data": "2026-10-15", "DataReferencia": "10/2026", "Mediana": 5.11, "Minimo": 4.9, "Maximo": 5.4, "numeroRespondentes": 98},
        {"Data": "2026-10-16", "DataReferencia": "11/2026", "Mediana": 5.12, "Minimo": 4.7, "Maximo": 5.4, "numeroRespondentes": 101},
        {"Data": "2026-10-16", "DataReferencia": "10/2026", "Mediana": 5.10, "Minimo": 4.89, "Maximo": 5.4, "numeroRespondentes": 101},
        {"Data": "2026-10-16", "DataReferencia": "12/2026", "Mediana": 5.15, "Minimo": 4.7, "Maximo": 5.5, "numeroRespondentes": 101},
    ]

    result = parse_focus_response(entries, ["10/2026", "11/2026"])

    assert [(r["month"], r["median"], r["survey_date"]) for r in result] == [
        ("10/2026", 5.10, "2026-10-16"),
        ("11/2026", 5.12, "2026-10-16"),
    ]


def test_parse_daily_moves():
    entries = [{"bid": b} for b in ("1.1229", "1.1214", "1.1226", "1.1238", "1.1212", "1.1195")]

    moves = parse_daily_moves(entries)

    assert moves == {"change_1d_pct": 0.13, "change_5d_pct": 0.3}
    assert parse_daily_moves(entries[:1]) == {}
