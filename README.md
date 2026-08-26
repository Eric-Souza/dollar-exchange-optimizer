# USD/BRL Daily Exchange Advisor

Sends a daily email recommending the best time to exchange USD salary into Brazilian reais (BRL), using BACEN PTAX rates and percentile-based analysis.

## How it works

1. Fetches official **PTAX** buy rates from the [Banco Central do Brasil](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/documentacao).
2. Computes your rate's percentile versus the last 30 days and the current month.
3. Sends a plain-text email with a recommendation: **EXCHANGE TODAY**, **GOOD DAY**, **NEUTRAL**, or **WAIT**.

The logic is tuned for receiving USD at the **start of the month** — days 1–10 are treated as your primary exchange window.

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in your credentials
python -m src.main
```

## Gmail App Password

1. Enable **2-Step Verification** on your Google account.
2. Go to [Google App Passwords](https://myaccount.google.com/apppasswords).
3. Create a password for "Mail" and copy the 16-character code.
4. Set `GMAIL_APP_PASSWORD` to that code (not your regular Gmail password).

## GitHub Actions setup

Push this repo to GitHub, then add these **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GMAIL_SENDER` | Gmail address that sends the email |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password |
| `EMAIL_RECIPIENT` | `eric.bh18souza@gmail.com` |
| `EXCHANGE_PERCENTILE_THRESHOLD` | Optional; default `75` |

The workflow runs **weekdays at 9:00 AM BRT** (12:00 UTC). You can also trigger it manually from the Actions tab via **Run workflow**.

## Running tests

```bash
pip install pytest
pytest tests/ -v
```

## Recommendation tiers

| 30-day percentile | Month percentile | Verdict |
|---|---|---|
| ≥ 85 | ≥ 80 | EXCHANGE TODAY |
| ≥ 70 | ≥ 65 | GOOD DAY |
| ≥ 50 | — | NEUTRAL |
| < 50 | < 40 | WAIT |

During days 1–10, if the 30-day percentile is ≥ 75 (configurable), the system leans toward **EXCHANGE TODAY**.

## Disclaimer

PTAX is a reference rate — your bank's effective rate may differ after spread and fees. Past percentiles do not predict future exchange rates. This tool does not account for IOF or other taxes.
