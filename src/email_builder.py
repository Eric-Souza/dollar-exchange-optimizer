"""Email composition and Gmail SMTP delivery."""

from __future__ import annotations

import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.analysis import ExchangeReport, Verdict, format_percentile

VERDICT_STYLES: dict[Verdict, dict[str, str]] = {
    Verdict.EXCHANGE_TODAY: {
        "label": "Exchange today",
        "bg": "#dcfce7",
        "border": "#16a34a",
        "text": "#14532d",
        "accent": "#16a34a",
    },
    Verdict.GOOD_DAY: {
        "label": "Good day",
        "bg": "#e0f2fe",
        "border": "#0284c7",
        "text": "#0c4a6e",
        "accent": "#0284c7",
    },
    Verdict.NEUTRAL: {
        "label": "Neutral",
        "bg": "#fef3c7",
        "border": "#d97706",
        "text": "#78350f",
        "accent": "#d97706",
    },
    Verdict.WAIT: {
        "label": "Wait",
        "bg": "#f1f5f9",
        "border": "#64748b",
        "text": "#334155",
        "accent": "#64748b",
    },
}


def _verdict_style(verdict: Verdict) -> dict[str, str]:
    return VERDICT_STYLES[verdict]


def _format_rate(rate: float | None) -> str:
    if rate is None:
        return "N/A"
    return f"R$ {rate:.4f}"


def _percentile_value(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(100.0, value))


def build_email_subject(report: ExchangeReport) -> str:
    return f"USD/BRL: {report.verdict.value} — {report.reference_date.strftime('%b %d')}"


def build_email_body(report: ExchangeReport) -> str:
    """Build plain-text email body (fallback for non-HTML clients)."""
    date_str = report.reference_date.strftime("%B %d, %Y")
    style = _verdict_style(report.verdict)

    lines = [
        f"USD/BRL Exchange Report — {date_str}",
        "",
        f">>> {style['label'].upper()} <<<",
        report.explanation,
        "",
    ]

    if report.today_rate is not None:
        lines.append(f"Today's rate (PTAX compra): {_format_rate(report.today_rate)}")
    else:
        lines.append("Today's rate: N/A (no quote published yet)")

    lines.append(f"30-day percentile: {format_percentile(report.rolling_30d_percentile)}")

    if report.month_stats and report.month_percentile is not None:
        lines.append(
            f"Month percentile:  {format_percentile(report.month_percentile)} "
            f"({report.month_stats.trading_days} trading days)"
        )
    else:
        lines.append("Month percentile:  N/A")

    if report.month_stats:
        stats = report.month_stats
        lines.extend(
            [
                "",
                "This month",
                f"  Best:  {_format_rate(stats.best_rate)} on {stats.best_date.strftime('%b %d')}",
                f"  Worst: {_format_rate(stats.worst_rate)} on {stats.worst_date.strftime('%b %d')}",
                f"  Avg:   {_format_rate(stats.average)}",
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
            "PTAX is a reference rate. Your bank may offer a different effective rate",
            "after spread/fees. Past percentiles do not predict future rates.",
        ]
    )

    return "\n".join(lines)


def _html_percentile_row(label: str, percentile: float | None, subtitle: str = "") -> str:
    pct = _percentile_value(percentile)
    pct_label = format_percentile(percentile)
    subtitle_html = (
        f'<span style="color:#64748b;font-size:12px;">{html.escape(subtitle)}</span>'
        if subtitle
        else ""
    )
    return f"""
    <tr>
      <td style="padding:10px 0;color:#475569;font-size:13px;width:140px;vertical-align:middle;">
        {html.escape(label)}<br>{subtitle_html}
      </td>
      <td style="padding:10px 0;vertical-align:middle;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <tr>
            <td style="background:#e2e8f0;border-radius:999px;height:10px;padding:0;">
              <div style="background:#0284c7;border-radius:999px;height:10px;width:{pct:.0f}%;max-width:100%;"></div>
            </td>
            <td style="width:48px;padding-left:10px;color:#0f172a;font-size:13px;font-weight:600;text-align:right;white-space:nowrap;">
              {html.escape(pct_label)}
            </td>
          </tr>
        </table>
      </td>
    </tr>
    """


def build_email_html(report: ExchangeReport) -> str:
    """Build HTML email body with verdict-first layout."""
    date_str = report.reference_date.strftime("%B %d, %Y")
    style = _verdict_style(report.verdict)
    rate_display = _format_rate(report.today_rate)
    month_subtitle = (
        f"{report.month_stats.trading_days} trading days"
        if report.month_stats and report.month_percentile is not None
        else ""
    )

    month_table = ""
    if report.month_stats:
        stats = report.month_stats
        month_table = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;margin-top:20px;">
          <tr>
            <td style="padding:14px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;text-align:center;width:33%;">
              <div style="font-size:11px;color:#166534;text-transform:uppercase;letter-spacing:0.06em;font-weight:600;">Best</div>
              <div style="font-size:18px;color:#14532d;font-weight:700;margin-top:4px;">{_format_rate(stats.best_rate)}</div>
              <div style="font-size:12px;color:#15803d;margin-top:2px;">{stats.best_date.strftime('%b %d')}</div>
            </td>
            <td style="width:8px;"></td>
            <td style="padding:14px;background:#fef2f2;border:1px solid #fecaca;border-radius:10px;text-align:center;width:33%;">
              <div style="font-size:11px;color:#991b1b;text-transform:uppercase;letter-spacing:0.06em;font-weight:600;">Worst</div>
              <div style="font-size:18px;color:#7f1d1d;font-weight:700;margin-top:4px;">{_format_rate(stats.worst_rate)}</div>
              <div style="font-size:12px;color:#b91c1c;margin-top:2px;">{stats.worst_date.strftime('%b %d')}</div>
            </td>
            <td style="width:8px;"></td>
            <td style="padding:14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;text-align:center;width:33%;">
              <div style="font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:0.06em;font-weight:600;">Average</div>
              <div style="font-size:18px;color:#0f172a;font-weight:700;margin-top:4px;">{_format_rate(stats.average)}</div>
              <div style="font-size:12px;color:#64748b;margin-top:2px;">this month</div>
            </td>
          </tr>
        </table>
        """

    notes_html = ""
    if report.early_month_note:
        notes_html += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:16px;">
          <tr>
            <td style="padding:12px 14px;background:#eff6ff;border-left:4px solid #2563eb;border-radius:8px;color:#1e3a8a;font-size:13px;line-height:1.5;">
              {html.escape(report.early_month_note)}
            </td>
          </tr>
        </table>
        """
    if report.end_of_month_note:
        notes_html += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:12px;">
          <tr>
            <td style="padding:12px 14px;background:#fff7ed;border-left:4px solid #ea580c;border-radius:8px;color:#7c2d12;font-size:13px;line-height:1.5;">
              {html.escape(report.end_of_month_note)}
            </td>
          </tr>
        </table>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(build_email_subject(report))}</title>
</head>
<body style="margin:0;padding:0;background:#eef2f7;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#eef2f7;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,0.08);">
          <tr>
            <td style="padding:24px 28px 18px;background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#ffffff;">
              <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.75;">Daily exchange report</div>
              <div style="font-size:22px;font-weight:700;margin-top:6px;">USD → BRL</div>
              <div style="font-size:13px;opacity:0.85;margin-top:4px;">{html.escape(date_str)}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 28px 8px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="padding:18px 20px;background:{style['bg']};border:1px solid {style['border']};border-radius:12px;">
                    <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:{style['accent']};">
                      Recommendation
                    </div>
                    <div style="font-size:24px;font-weight:800;color:{style['text']};margin-top:6px;line-height:1.2;">
                      {html.escape(style['label'])}
                    </div>
                    <div style="font-size:14px;color:{style['text']};margin-top:10px;line-height:1.55;opacity:0.92;">
                      {html.escape(report.explanation)}
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 28px 0;">
              <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#64748b;margin-bottom:8px;">
                Today
              </div>
              <div style="font-size:32px;font-weight:800;color:#0f172a;letter-spacing:-0.02em;">{html.escape(rate_display)}</div>
              <div style="font-size:12px;color:#64748b;margin-top:4px;">PTAX compra (reference rate when selling USD)</div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 28px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                {_html_percentile_row("30-day rank", report.rolling_30d_percentile, "vs last 30 calendar days")}
                {_html_percentile_row("Month rank", report.month_percentile, month_subtitle)}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 28px 24px;">
              <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#64748b;margin-bottom:4px;">
                This month
              </div>
              {month_table}
              {notes_html}
            </td>
          </tr>
          <tr>
            <td style="padding:16px 28px 24px;border-top:1px solid #e2e8f0;background:#f8fafc;">
              <div style="font-size:12px;color:#64748b;line-height:1.6;">
                PTAX is a reference rate. Your bank may offer a different effective rate after spread and fees.
                Past percentiles do not predict future exchange rates.
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


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

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = build_email_subject(report)
    msg.attach(MIMEText(build_email_body(report), "plain", "utf-8"))
    msg.attach(MIMEText(build_email_html(report), "html", "utf-8"))

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
