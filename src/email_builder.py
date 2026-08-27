"""Email composition and Gmail SMTP delivery."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.analysis import ExchangeReport, format_percentile


def build_email_body(report: ExchangeReport) -> str:
    """Build plain-text email body from an exchange report."""
    date_str = report.reference_date.strftime("%B %d, %Y")
    lines = [f"USD/BRL Exchange Report — {date_str}", ""]

    lines.append("TODAY")
    if report.today_rate is not None:
        lines.append(f"  Rate (PTAX compra): R$ {report.today_rate:.4f}")
    else:
        lines.append("  Rate (PTAX compra): N/A (no quote for today yet)")
    lines.append(f"  30-day percentile:   {format_percentile(report.rolling_30d_percentile)}")

    if report.month_stats:
        month_pct_str = format_percentile(report.month_percentile)
        if report.month_percentile is not None:
            month_pct_str += f" ({report.month_stats.trading_days} trading days)"
        lines.append(f"  Month percentile:    {month_pct_str}")
    else:
        lines.append("  Month percentile:    N/A")

    if report.month_stats:
        stats = report.month_stats
        lines.extend(
            [
                "",
                "THIS MONTH",
                f"  Best:  R$ {stats.best_rate:.4f} on {stats.best_date.strftime('%b %d')}",
                f"  Worst: R$ {stats.worst_rate:.4f} on {stats.worst_date.strftime('%b %d')}",
                f"  Avg:   R$ {stats.average:.4f}",
            ]
        )

    lines.extend(
        [
            "",
            f"RECOMMENDATION: {report.verdict.value}",
            f"  {report.explanation}",
        ]
    )

    if report.early_month_note:
        lines.extend(["", report.early_month_note])

    if report.end_of_month_note:
        lines.extend(["", report.end_of_month_note])

    lines.extend(
        [
            "",
            "---",
            "Note: PTAX is a reference rate. Your bank may offer a different",
            "effective rate after spread/fees. Past percentiles do not predict",
            "future rates.",
        ]
    )

    return "\n".join(lines)


def build_email_subject(report: ExchangeReport) -> str:
    return f"USD/BRL: {report.verdict.value} — {report.reference_date.strftime('%b %d')}"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(
            f"Missing required environment variable: {name}. "
            "Set it in GitHub Actions secrets or your local .env file."
        )
    return value


def _normalize_app_password(password: str) -> str:
    """Gmail app passwords are often copied with spaces (xxxx xxxx xxxx xxxx)."""
    return password.replace(" ", "")


def send_email(
    report: ExchangeReport,
    sender: str | None = None,
    password: str | None = None,
    recipient: str | None = None,
) -> None:
    """Send the exchange report via Gmail SMTP."""
    sender = (sender or _require_env("GMAIL_SENDER")).strip()
    password = _normalize_app_password(password or _require_env("GMAIL_APP_PASSWORD"))
    recipient = (recipient or os.environ.get("EMAIL_RECIPIENT", sender)).strip()

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = build_email_subject(report)
    msg.attach(MIMEText(build_email_body(report), "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise smtplib.SMTPAuthenticationError(
            exc.smtp_code,
            "Gmail rejected the login. Use a Gmail App Password (not your normal "
            "password), ensure GMAIL_SENDER matches the account that created it, "
            "and regenerate the app password at https://myaccount.google.com/apppasswords "
            f"Original error: {exc.smtp_error!r}",
        ) from exc
