"""Entry point for the USD/BRL daily exchange advisor."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from src.ai_advisor import ai_enabled, get_ai_opinion
from src.analysis import Verdict, build_report
from src.email_builder import build_email_body, build_email_subject, send_email
from src.market_context import fetch_market_context
from src.ptax import fetch_history

# Brazil has had no DST since 2019; avoids needing tzdata on Windows.
BRT = timezone(timedelta(hours=-3))


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

    dry_run = "--dry-run" in sys.argv[1:]

    reference = datetime.now(BRT).date()
    early_month_threshold = _env_float("EXCHANGE_PERCENTILE_THRESHOLD", 75.0)

    history, today_quote = fetch_history(reference)

    report = build_report(
        history=history,
        reference=reference,
        today_quote=today_quote,
        early_month_threshold=early_month_threshold,
    )
    ai = None
    if ai_enabled() and report.verdict != Verdict.NO_QUOTE:
        ai = get_ai_opinion(report, history, fetch_market_context(reference))
    ai_label = ai.verdict.value if ai and ai.verdict else "n/a"

    if dry_run:
        print(build_email_subject(report, ai))
        print()
        print(build_email_body(report, ai))
        return 0

    send_email(report, ai)
    print(f"Email sent: {report.verdict.value} (AI: {ai_label}) for {reference.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
