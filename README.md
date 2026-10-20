# USD/BRL Daily Exchange Advisor

Sends a daily email recommending the best time to exchange USD salary into Brazilian reais (BRL), using BACEN PTAX rates and percentile-based analysis.

## How it works

1. Fetches official **PTAX** buy rates from the [Banco Central do Brasil](https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/documentacao): 120 days of closing rates plus today's latest bulletin (opening ~10:00, intermediate ~11:00/12:00, closing ~13:05 BRT). Before the first bulletin, it falls back to a live market quote from [AwesomeAPI](https://docs.awesomeapi.com.br/api-de-moedas).
2. Computes today's percentile versus the last 30 days, the current month and the last 90 days, plus trend (5-day vs 20-day average), z-score, recent changes and volatility.
3. Gathers free market context: BACEN Focus survey, Selic and the last Copom decision, peer currencies and the last two days of FX headlines.
4. Optionally asks **Google Gemini** (free tier) for a second opinion and a 5-trading-day outlook, using all of the above plus its own past track record.
5. Sends one email per day with a recommendation (**EXCHANGE TODAY**, **GOOD DAY**, **NEUTRAL**, **WAIT**, or **NO QUOTE** for holidays / nothing published yet) and:
   - an at-a-glance strip (today's rate, gap to the month best, AI outlook, weekdays left),
   - your salary in BRL today vs the month best and average (set `SALARY_USD`),
   - a 30-trading-day bar chart, market context, signals and top headlines,
   - the AI's opinion with its hit rate on past 5-day outlooks.

The logic is tuned for receiving USD at the **start of the month** — days 1–10 are treated as your primary exchange window.

## Local setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in your credentials
python -m src.main --dry-run   # print the report instead of emailing it
python -m src.main
```

## Gmail App Password

1. Enable **2-Step Verification** on your Google account.
2. Go to [Google App Passwords](https://myaccount.google.com/apppasswords).
3. Create a password for "Mail" and copy the 16-character code.
4. Set `GMAIL_APP_PASSWORD` to that code (not your regular Gmail password).

## AI second opinion (Gemini, free)

1. Create a free API key at [Google AI Studio](https://aistudio.google.com/apikey).
2. Set `GEMINI_API_KEY` locally (`.env`) and as a GitHub secret.

Without a key, the email is sent with the rule-based verdict only. If the Gemini call fails, the email still goes out.

The AI receives today's numbers, 90 days of PTAX closings and context from free, keyless sources:

| Context | Source |
|---|---|
| Market median USD/BRL expectation for this and next month | BACEN Focus survey |
| Selic target rate and the last Copom decision | BACEN SGS series 432, BCB Copom API |
| 1-day / 5-day moves of EUR/USD, USD/MXN, USD/CLP, USD/ZAR | AwesomeAPI |
| Headlines from the last 2 days (Brazilian FX and global dollar) | Google News RSS |
| Its own past 5-day outlooks, scored against real closings | `data/forecasts.json` |
| More news and upcoming events (FOMC, Copom, payrolls, IPCA) | Google Search grounding, when the key has quota |
| FOMC meeting calendar | federalreserve.gov, read via Gemini's URL context tool |

It returns its own verdict, drivers, risks and an outlook (up / down / sideways over 5 trading days). Short-term FX direction is close to a coin flip, so treat the outlook as context. The email shows how often past outlooks were right.

**Picking the model.** Free-tier access changes over time, so each run lists the models the key can use and tries them best first: pro, then flash, then flash-lite, newest version first. For each model it tries Search + URL context, then URL context only, then no tools. A tool that gets refused (429/400) is dropped for the rest of the run. Overloaded models (503 or timeout) get one more try at the end. The whole AI step is capped at about 6 minutes; if nothing answers, the email still goes out with the rule-based verdict.

**Track record.** After each email is sent, the workflow appends that day's rate, verdicts and AI outlook to `data/forecasts.json` and commits it to `main` (`[skip ci]`). Dry runs never write to it.

Optional settings: `GEMINI_MODEL` (pin a model to try first), `GEMINI_USE_SEARCH` (default `true`), `SALARY_USD`.

Note: on Gemini's free tier, Google may use prompts to improve its products. The prompts here contain only public market data.

## GitHub Actions setup

Push this repo to GitHub, then add these **repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `GMAIL_SENDER` | Gmail address that sends the email |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password |
| `EMAIL_RECIPIENT` | `eric.bh18souza@gmail.com` |
| `EXCHANGE_PERCENTILE_THRESHOLD` | Optional; default `75` |
| `GEMINI_API_KEY` | Optional; enables the AI second opinion |
| `GEMINI_MODEL` | Optional; model to try first (default: best available) |
| `SALARY_USD` | Optional; monthly salary in USD for the BRL calculator |

The workflow is scheduled for **weekdays at ~10:23 BRT** (13:23 UTC), after BACEN's first bulletin. GitHub often delays scheduled runs by an hour or more; the email shows which quote it used (intraday bulletin, closing, or market bid). You can also trigger it manually from the Actions tab via **Run workflow**. The workflow needs `contents: write` (already set in the YAML) to commit the forecast log.

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

A **NEUTRAL** day is upgraded to **GOOD DAY** when today's rate is in the top 20% of the last 90 days. The explanation also mentions the trend (5-day vs 20-day average) when it matters. **NO QUOTE** means no rate was available (holiday or before the first bulletin).

## Disclaimer

PTAX is a reference rate — your bank's effective rate may differ after spread and fees. Past percentiles do not predict future exchange rates. This tool does not account for IOF or other taxes.
