"""Entry point for the USD/BRL daily exchange advisor."""

from __future__ import annotations

import os
import sys
from datetime import date

from src.analysis import build_report
from src.email_builder import send_email
from src.ptax import fetch_month_rates, fetch_rolling_rates


def _env_float(name: str, default: float) -> float:
    """Read a float env var, treating missing or blank values as default."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return float(value)


def _load_dotenv() -> None:
    """Load .env file if present (for local runs)."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_dotenv()

    reference = date.today()
    early_month_threshold = _env_float("EXCHANGE_PERCENTILE_THRESHOLD", 75.0)

    month_rates = fetch_month_rates(reference)
    rolling_rates = fetch_rolling_rates(days=30, reference=reference)

    report = build_report(
        month_rates=month_rates,
        rolling_rates=rolling_rates,
        reference=reference,
        early_month_threshold=early_month_threshold,
    )

    send_email(report)
    print(f"Email sent: {report.verdict.value} for {reference.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
