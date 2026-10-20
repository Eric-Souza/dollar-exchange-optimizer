"""Email composition and Gmail SMTP delivery."""

from __future__ import annotations

import html
import os
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from src.ai_advisor import AIOpinion, Outlook
from src.analysis import ExchangeReport, Signals, Verdict, format_percentile, remaining_weekdays
from src.forecast_log import TrackRecord
from src.news import Headline
from src.ptax import DailyRate

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
    Verdict.NO_QUOTE: {
        "label": "No quote yet",
        "bg": "#f8fafc",
        "border": "#cbd5e1",
        "text": "#475569",
        "accent": "#94a3b8",
    },
}

TREND_LABELS = {"up": "Rising", "down": "Falling", "flat": "Flat"}
CHART_DAYS = 30
CHART_HEIGHT_PX = 90
MAX_EMAIL_HEADLINES = 6
SPARK_CHARS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class EmailExtras:
    """Optional context shown in the email beyond the core report."""

    history: list[DailyRate] = field(default_factory=list)
    market: dict[str, Any] = field(default_factory=dict)
    headlines: list[Headline] = field(default_factory=list)
    track_record: TrackRecord | None = None
    salary_usd: float | None = None


def _verdict_style(verdict: Verdict) -> dict[str, str]:
    return VERDICT_STYLES[verdict]


def _format_rate(rate: float | None) -> str:
    if rate is None:
        return "N/A"
    return f"R$ {rate:.4f}"


def _format_brl(amount: float) -> str:
    return f"R$ {amount:,.2f}"


def _percentile_value(value: float | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(100.0, value))


def _format_signed(value: float | None, suffix: str = "%") -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}{suffix}"


def _signal_lines(signals: Signals) -> list[tuple[str, str]]:
    """Label/value pairs for the signals section."""
    trend = TREND_LABELS.get(signals.trend or "", "N/A")
    if signals.ma5 is not None and signals.ma20 is not None:
        trend += f" (5d avg {signals.ma5:.4f} vs 20d {signals.ma20:.4f})"
    volatility = (
        f"{signals.volatility_20d_pct:.2f}% per day (20d)"
        if signals.volatility_20d_pct is not None
        else "N/A"
    )
    return [
        ("90-day rank", format_percentile(signals.quarter_percentile)),
        ("Trend", trend),
        ("vs 30d average", _format_signed(signals.zscore_30d, " σ")),
        ("1-day change", _format_signed(signals.change_1d_pct)),
        ("5-day change", _format_signed(signals.change_5d_pct)),
        ("Volatility", volatility),
    ]


def _ai_headline(ai: AIOpinion) -> str:
    verdict = ai.verdict.value if ai.verdict else "No verdict"
    return f"{verdict} ({ai.confidence} confidence)" if ai.confidence else verdict


OUTLOOK_LABELS = {
    "up": "Dollar may rise ↑ — waiting could pay off",
    "down": "Dollar may fall ↓ — waiting could cost you",
    "sideways": "Dollar likely flat → — little gain from waiting",
}
OUTLOOK_ARROWS = {"up": "↑ Up", "down": "↓ Down", "sideways": "→ Flat"}


def _outlook_headline(outlook: Outlook) -> str:
    label = OUTLOOK_LABELS[outlook.direction]
    return f"{label} ({outlook.confidence} confidence)" if outlook.confidence else label


# --- Derived content shared by the text and HTML versions -------------------


def _glance_items(report: ExchangeReport, ai: AIOpinion | None) -> list[tuple[str, str, str]]:
    """(label, value, detail) cells for the at-a-glance strip."""
    signals = report.signals
    stats = report.month_stats
    items = [
        (
            "Today",
            _format_rate(report.today_rate),
            "1d " + _format_signed(signals.change_1d_pct) if signals else "",
        )
    ]
    if stats and report.today_rate is not None:
        gap = (report.today_rate / stats.best_rate - 1) * 100
        detail = "today is the best" if gap >= 0 else f"{gap:+.2f}% vs best"
        items.append(("Month best", _format_rate(stats.best_rate), detail))
    if ai and ai.outlook:
        items.append(
            ("AI 5-day", OUTLOOK_ARROWS[ai.outlook.direction], f"{ai.outlook.confidence or '?'} conf.")
        )
    left = remaining_weekdays(report.reference_date)
    items.append(("Weekdays left", str(left), f"after {report.reference_date.strftime('%b %d')}"))
    return items


def _chart_points(report: ExchangeReport, history: list[DailyRate]) -> list[tuple[date, float]]:
    """Last trading days' closings plus today's quote."""
    ref = report.reference_date
    points = [(r.date, r.rate) for r in sorted(history, key=lambda r: r.date) if r.date < ref]
    if report.today_rate is not None:
        points.append((ref, report.today_rate))
    return points[-CHART_DAYS:]


def _sparkline(rates: list[float]) -> str:
    low, high = min(rates), max(rates)
    span = (high - low) or 1.0
    return "".join(SPARK_CHARS[round((r - low) / span * (len(SPARK_CHARS) - 1))] for r in rates)


def _salary_lines(report: ExchangeReport, salary: float | None) -> list[str]:
    if not salary or report.today_rate is None:
        return []
    today = salary * report.today_rate
    lines = [f"US$ {salary:,.0f} today → {_format_brl(today)}"]
    stats = report.month_stats
    if stats:
        best = salary * stats.best_rate
        if stats.best_date == report.reference_date:
            lines.append("Today is the month's best so far.")
        else:
            lines.append(
                f"At the month best ({_format_rate(stats.best_rate)}, "
                f"{stats.best_date.strftime('%b %d')}): {_format_brl(best)} "
                f"({_format_brl(best - today)} more)"
            )
        diff = today - salary * stats.average
        lines.append(f"vs the month average: {'+' if diff >= 0 else '−'}{_format_brl(abs(diff))}")
    lines.append(f"Every R$ 0.01 move = {_format_brl(salary * 0.01)}")
    return lines


def _market_lines(report: ExchangeReport, market: dict[str, Any]) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    if not market:
        return lines
    for focus in market.get("focus_survey_usdbrl_by_month", []):
        value = f"{_format_rate(focus['median'])} median"
        if report.today_rate:
            value += f" ({(focus['median'] / report.today_rate - 1) * 100:+.1f}% vs today)"
        lines.append((f"Focus {focus['month']}", value))
    if "selic_target_pct" in market:
        lines.append(("Selic target", f"{market['selic_target_pct']:.2f}%"))
    copom = market.get("last_copom")
    if copom:
        lines.append(("Last Copom", f"{str(copom['date'])[:10]} — {copom['decision']}"))
    if report.signals:
        lines.append(
            (
                "USD/BRL",
                f"1d {_format_signed(report.signals.change_1d_pct)} · "
                f"5d {_format_signed(report.signals.change_5d_pct)}",
            )
        )
    for pair, moves in market.get("peer_currencies", {}).items():
        lines.append(
            (
                pair.replace("-", "/"),
                f"1d {_format_signed(moves.get('change_1d_pct'))} · "
                f"5d {_format_signed(moves.get('change_5d_pct'))}",
            )
        )
    return lines


def _track_record_line(track: TrackRecord | None) -> str | None:
    if track is None or (not track.scored and not track.pending):
        return None
    if not track.scored:
        return f"AI track record: no 5-day outlook scored yet ({track.pending} pending)."
    last = track.scored[0]
    return (
        f"AI track record: {track.hits}/{len(track.scored)} 5-day outlooks right "
        f"({track.hit_rate:.0f}%), {track.pending} pending. Last scored: "
        f"{last.date.strftime('%b %d')} said {last.direction}, it went {last.actual} "
        f"({last.change_pct:+.2f}%) — {'✓' if last.hit else '✗'}"
    )


# --- Plain text --------------------------------------------------------------


def build_email_subject(report: ExchangeReport, ai: AIOpinion | None = None) -> str:
    subject = f"USD/BRL: {report.verdict.value} — {report.reference_date.strftime('%b %d')}"
    if report.today_rate is not None:
        subject += f" · {report.today_rate:.4f}"
    if ai and ai.verdict and ai.verdict != report.verdict:
        subject += f" (AI: {ai.verdict.value})"
    return subject


def build_email_body(
    report: ExchangeReport, ai: AIOpinion | None = None, extras: EmailExtras | None = None
) -> str:
    """Build plain-text email body (fallback for non-HTML clients)."""
    extras = extras or EmailExtras()
    date_str = report.reference_date.strftime("%B %d, %Y")
    style = _verdict_style(report.verdict)

    lines = [
        f"USD/BRL Exchange Report — {date_str}",
        "",
        f">>> {style['label'].upper()} <<<",
        report.explanation,
        "",
        " | ".join(f"{label}: {value}" + (f" ({detail})" if detail else "")
                   for label, value, detail in _glance_items(report, ai)),
        "",
    ]

    if report.today_rate is not None:
        lines.append(f"Today's rate: {_format_rate(report.today_rate)} — {report.rate_source}")
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

    salary = _salary_lines(report, extras.salary_usd)
    if salary:
        lines.extend(["", "Your salary"])
        lines.extend(f"  {line}" for line in salary)

    points = _chart_points(report, extras.history)
    if len(points) >= 2:
        rates = [rate for _, rate in points]
        lines.extend(
            [
                "",
                f"Last {len(points)} trading days ({points[0][0].strftime('%b %d')} → today)",
                f"  {_sparkline(rates)}  low {min(rates):.4f} · high {max(rates):.4f}",
            ]
        )

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

    if ai:
        lines.extend(["", f"AI second opinion: {_ai_headline(ai)}", ai.summary])
        for title, items in (("Drivers", ai.drivers), ("Risks", ai.risks)):
            if items:
                lines.append(f"  {title}:")
                lines.extend(f"  - {item}" for item in items)
        if ai.outlook:
            lines.extend(
                ["", f"Next 5 trading days: {_outlook_headline(ai.outlook)}", ai.outlook.reasoning]
            )
            if ai.outlook.key_events:
                lines.append("  Upcoming events:")
                lines.extend(f"  - {event}" for event in ai.outlook.key_events)
            lines.append("  (Short-term FX calls are close to a coin flip; treat as context.)")
        if ai.sources:
            lines.append("  Sources:")
            lines.extend(f"  - {title}: {uri}" for title, uri in ai.sources)
        lines.append(f"  via {ai.via}")

    track = _track_record_line(extras.track_record)
    if track:
        lines.extend(["", track])

    market = _market_lines(report, extras.market)
    if market:
        lines.extend(["", "Market context"])
        lines.extend(f"  {label}: {value}" for label, value in market)

    if report.signals:
        lines.extend(["", "Signals"])
        lines.extend(f"  {label}: {value}" for label, value in _signal_lines(report.signals))

    if extras.headlines:
        lines.extend(["", "Headlines"])
        for h in extras.headlines[:MAX_EMAIL_HEADLINES]:
            lines.append(f"  - {h.title} ({h.source})" if h.source else f"  - {h.title}")

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


# --- HTML --------------------------------------------------------------------


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


def _html_section_label(text: str) -> str:
    return (
        '<div style="font-size:12px;font-weight:700;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:#64748b;margin-bottom:4px;">{html.escape(text)}</div>'
    )


def _html_section(label: str, content: str) -> str:
    return f"""
          <tr>
            <td style="padding:16px 28px 0;">
              {_html_section_label(label)}
              {content}
            </td>
          </tr>"""


def _html_kv_table(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"""
                <tr>
                  <td style="padding:6px 0;color:#475569;font-size:13px;width:140px;vertical-align:top;">{html.escape(label)}</td>
                  <td style="padding:6px 0;color:#0f172a;font-size:13px;font-weight:600;">{html.escape(value)}</td>
                </tr>"""
        for label, value in rows
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;">{cells}</table>'
    )


def _html_glance(report: ExchangeReport, ai: AIOpinion | None) -> str:
    items = _glance_items(report, ai)
    cells = '<td style="width:6px;"></td>'.join(
        f"""
                  <td style="padding:10px 8px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;text-align:center;vertical-align:top;">
                    <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;font-weight:700;">{html.escape(label)}</div>
                    <div style="font-size:16px;color:#0f172a;font-weight:800;margin-top:4px;white-space:nowrap;">{html.escape(value)}</div>
                    <div style="font-size:11px;color:#64748b;margin-top:2px;">{html.escape(detail)}</div>
                  </td>"""
        for label, value, detail in items
    )
    return f"""
          <tr>
            <td style="padding:16px 28px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;">
                <tr>{cells}
                </tr>
              </table>
              <div style="font-size:11px;color:#64748b;margin-top:6px;">{html.escape(report.rate_source or "No quote available")} · reference rate when selling USD</div>
            </td>
          </tr>"""


def _html_chart(report: ExchangeReport, history: list[DailyRate]) -> str:
    """Bar chart built from table cells (renders in Gmail, unlike SVG)."""
    points = _chart_points(report, history)
    if len(points) < 2:
        return ""
    rates = [rate for _, rate in points]
    low, high = min(rates), max(rates)
    span = (high - low) or 1.0
    best = report.month_stats.best_date if report.month_stats else None
    month = report.reference_date.month

    bars = []
    for day, rate in points:
        height = 6 + round((rate - low) / span * (CHART_HEIGHT_PX - 6))
        if day == report.reference_date:
            color = "#0f172a"
        elif day == best:
            color = "#16a34a"
        elif day.month == month:
            color = "#7dd3fc"
        else:
            color = "#cbd5e1"
        bars.append(
            f'<td style="vertical-align:bottom;padding:0 1px;" title="{day:%b %d}: {rate:.4f}">'
            f'<div style="height:{height}px;background:{color};border-radius:2px 2px 0 0;'
            f'font-size:0;line-height:0;">&nbsp;</div></td>'
        )
    legend = (
        '<span style="color:#0f172a;">■</span> today · '
        '<span style="color:#16a34a;">■</span> month best · '
        '<span style="color:#7dd3fc;">■</span> this month · '
        '<span style="color:#cbd5e1;">■</span> earlier'
    )
    return _html_section(
        f"Last {len(points)} trading days",
        f"""
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;height:{CHART_HEIGHT_PX}px;">
                <tr>{"".join(bars)}</tr>
              </table>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:4px;">
                <tr>
                  <td style="font-size:11px;color:#64748b;">{points[0][0]:%b %d}</td>
                  <td style="font-size:11px;color:#64748b;text-align:center;">low {low:.4f} · high {high:.4f}</td>
                  <td style="font-size:11px;color:#64748b;text-align:right;">today</td>
                </tr>
              </table>
              <div style="font-size:11px;color:#64748b;margin-top:4px;">{legend}</div>""",
    )


def _html_salary(report: ExchangeReport, salary: float | None) -> str:
    lines = _salary_lines(report, salary)
    if not lines:
        return ""
    head, *rest = lines
    details = "".join(f'<div style="margin-top:3px;">{html.escape(line)}</div>' for line in rest)
    return _html_section(
        "Your salary",
        f"""
              <div style="padding:14px 16px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;color:#14532d;font-size:13px;">
                <div style="font-size:18px;font-weight:800;">{html.escape(head)}</div>
                {details}
              </div>""",
    )


def _html_list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    entries = "".join(f'<li style="margin:2px 0;">{html.escape(item)}</li>' for item in items)
    return (
        f'<div style="font-size:12px;font-weight:700;margin-top:10px;">{html.escape(title)}</div>'
        f'<ul style="margin:4px 0 0;padding-left:18px;">{entries}</ul>'
    )


def _html_ai(ai: AIOpinion | None, track: TrackRecord | None = None) -> str:
    if ai is None:
        return ""
    sources = ""
    if ai.sources:
        links = " · ".join(
            f'<a href="{html.escape(uri)}" style="color:#6d28d9;">{html.escape(title)}</a>'
            for title, uri in ai.sources
        )
        sources = f'<div style="font-size:11px;margin-top:10px;">Sources: {links}</div>'
    outlook = ""
    if ai.outlook:
        outlook = f"""
                    <div style="margin-top:14px;padding-top:12px;border-top:1px solid #ddd6fe;">
                      <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#7c3aed;">
                        Next 5 trading days
                      </div>
                      <div style="font-size:15px;font-weight:700;margin-top:4px;">{html.escape(_outlook_headline(ai.outlook))}</div>
                      <div style="margin-top:6px;">{html.escape(ai.outlook.reasoning)}</div>
                      {_html_list("Upcoming events", ai.outlook.key_events)}
                      <div style="font-size:11px;color:#6b21a8;margin-top:8px;opacity:0.8;">
                        Short-term FX calls are close to a coin flip — treat this as context, not a forecast to bet on.
                      </div>
                    </div>"""
    track_line = _track_record_line(track)
    track_html = (
        f'<div style="font-size:12px;margin-top:10px;padding:8px 10px;background:#ede9fe;border-radius:8px;">'
        f"{html.escape(track_line)}</div>"
        if track_line
        else ""
    )
    return f"""
          <tr>
            <td style="padding:16px 28px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                <tr>
                  <td style="padding:16px 18px;background:#f5f3ff;border:1px solid #c4b5fd;border-radius:12px;color:#3b0764;font-size:13px;line-height:1.55;">
                    <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#7c3aed;">
                      AI second opinion
                    </div>
                    <div style="font-size:18px;font-weight:800;margin-top:4px;">{html.escape(_ai_headline(ai))}</div>
                    <div style="margin-top:8px;">{html.escape(ai.summary)}</div>
                    {_html_list("Drivers", ai.drivers)}
                    {_html_list("Risks", ai.risks)}
                    {outlook}
                    {track_html}
                    {sources}
                    <div style="font-size:11px;color:#7c3aed;margin-top:10px;">via {html.escape(ai.via)}</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""


def _html_market(report: ExchangeReport, market: dict[str, Any]) -> str:
    rows = _market_lines(report, market)
    if not rows:
        return ""
    note = (
        '<div style="font-size:11px;color:#64748b;margin-top:4px;">'
        "Focus = BACEN survey median of ~100 banks. EUR/USD up = weaker global dollar; "
        "USD/peer up = stronger dollar vs that currency.</div>"
    )
    return _html_section("Market context", _html_kv_table(rows) + note)


def _html_headlines(headlines: list[Headline]) -> str:
    if not headlines:
        return ""
    items = "".join(
        f'<li style="margin:4px 0;"><a href="{html.escape(h.link)}" style="color:#0f172a;text-decoration:none;">'
        f"{html.escape(h.title)}</a>"
        + (f' <span style="color:#94a3b8;">· {html.escape(h.source)}</span>' if h.source else "")
        + "</li>"
        for h in headlines[:MAX_EMAIL_HEADLINES]
    )
    return _html_section(
        "Headlines (last 2 days)",
        f'<ul style="margin:4px 0 0;padding-left:18px;font-size:13px;line-height:1.45;">{items}</ul>',
    )


def build_email_html(
    report: ExchangeReport, ai: AIOpinion | None = None, extras: EmailExtras | None = None
) -> str:
    """Build HTML email body with verdict-first layout."""
    extras = extras or EmailExtras()
    date_str = report.reference_date.strftime("%B %d, %Y")
    style = _verdict_style(report.verdict)
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
               style="border-collapse:collapse;margin-top:8px;">
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

    signals = _html_section("Signals", _html_kv_table(_signal_lines(report.signals))) if report.signals else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(build_email_subject(report, ai))}</title>
</head>
<body style="margin:0;padding:0;background:#eef2f7;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#eef2f7;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,0.08);">
          <tr>
            <td style="padding:24px 28px 18px;background:#0f172a;background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#ffffff;">
              <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.75;">Daily exchange report</div>
              <div style="font-size:22px;font-weight:700;margin-top:6px;">USD → BRL</div>
              <div style="font-size:13px;opacity:0.85;margin-top:4px;">{html.escape(date_str)}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 28px 0;">
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
          {_html_glance(report, ai)}
          {_html_salary(report, extras.salary_usd)}
          <tr>
            <td style="padding:12px 28px 0;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                {_html_percentile_row("30-day rank", report.rolling_30d_percentile, "vs last 30 calendar days")}
                {_html_percentile_row("Month rank", report.month_percentile, month_subtitle)}
              </table>
            </td>
          </tr>
          {_html_chart(report, extras.history)}
          {_html_ai(ai, extras.track_record)}
          {_html_market(report, extras.market)}
          {signals}
          {_html_headlines(extras.headlines)}
          <tr>
            <td style="padding:16px 28px 24px;">
              {_html_section_label("This month")}
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
    ai: AIOpinion | None = None,
    extras: EmailExtras | None = None,
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
    msg["Subject"] = build_email_subject(report, ai)
    msg.attach(MIMEText(build_email_body(report, ai, extras), "plain", "utf-8"))
    msg.attach(MIMEText(build_email_html(report, ai, extras), "html", "utf-8"))

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
