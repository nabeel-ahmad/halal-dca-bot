# Halal Portfolio Weekly Review

A read-only weekly email about your Binance stocks/ETF holdings: a Sharia
compliance re-check, concentration warnings, BDS-boycott/Pentagon-contractor
flags, and a shortlist of Sharia-compliant candidate stocks & ETFs for new
money. **It never places an order** — see
[weekly_portfolio_review.py](weekly_portfolio_review.py).

**This is not financial advice.** It automates mechanical rules (a Musaffa
screener filter you chose, a cash-reserve target, BDS/DoD-contractor exclusion
lists) — it doesn't exercise investment judgment on your behalf, and the
candidate lists it surfaces are not a recommendation to buy.

## What's here

| File | Purpose |
|---|---|
| `weekly_portfolio_review.py` | Entry point. Re-checks current holdings' Sharia compliance (Musaffa + Zoya), flags concentration risk and BDS/Pentagon-contractor exposure, and emails a shortlist of A/A+-rated candidate stocks & ETFs matching a Musaffa screener filter, with a mechanical $ split of idle cash. |
| `binance_equity.py` | Binance Stocks Trading API client (signed requests, holdings, prices) + SMTP sender, shared by the review script. |
| `musaffa_recommendations.py` | Headless-browser (Playwright) scraper for the Musaffa screener and per-ticker Halal Rating grade — the screener table is JS-rendered, so a plain HTTP request won't see it. |
| `ethics_screens.py` | Small, manually-curated reference lists: BDS priority-boycott targets (sourced from bdsmovement.net) and major US DoD prime contractors (sourced from public federal contract data). Used to warn on existing holdings and exclude from new candidates. |
| `.env.example` | Template for local env vars — copy to `.env` for local testing only, never commit a real one. |
| `.github/workflows/weekly-portfolio-review.yml` | Weekly schedule (Sunday 05:00 UTC / 10:00 PKT) + manual trigger. |

## Why a self-hosted runner

Binance blocks requests from GitHub-hosted-runner (and other datacenter/cloud)
IP ranges with HTTP 451, regardless of account region. The workflow runs on a
self-hosted runner on your own Mac (`runs-on: [self-hosted, macOS, ARM64]`)
instead. That means:

- The Mac needs to be on and the runner service running (`actions-runner/`,
  installed as a launchd service) for the scheduled run to fire.
- Dependencies install into a persistent venv at
  `/Users/hk-nabeel/.venvs/halal-dca-bot` (kept outside the repo checkout,
  since `actions/checkout`'s default clean step wipes untracked files inside
  it) rather than via `actions/setup-python`, which assumes GitHub-hosted
  runners' toolcache layout.
- Chromium is installed once via `playwright install chromium` in that same
  venv (needed by `musaffa_recommendations.py` — Musaffa's screener table is
  JS-rendered).

## Setup

1. Add these as repo secrets (Settings → Secrets and variables → Actions):
   `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `SMTP_HOST`, `SMTP_USER`,
   `SMTP_PASSWORD`, `EMAIL_TO`.
2. Binance API key: Spot & Stocks trading permission only — leave
   withdrawals and margin/futures off. This project never places orders, so
   even read-only permission is enough if Binance offers it.
3. SMTP: an app password (e.g. Gmail), not your real account password.

## Ethics screens

`ethics_screens.py` holds two short, manually-curated lists — not large
structured databases, since neither BDS nor a DoD-contractor registry
publishes one as a clean API. Re-verify against the source pages
periodically; a stale list under- or over-flags silently otherwise.

- **BDS**: named priority-boycott targets from
  https://bdsmovement.net/get-involved/what-to-boycott — reported as "this
  named source lists this company," not as Claude's own judgment.
- **Pentagon**: major US DoD prime contractors by contract value, from
  public USAspending.gov federal award data.

An AIPAC-affiliation screen was explicitly **not** built — there's no
reliable public database mapping companies to political-lobby affiliation,
and inferring one would mean making subjective, contestable claims about
specific real companies.

## Safety

- Read-only. No trade is ever executed by this project.
- Binance has no API for marking watchlist favorites — the weekly email
  lists tickers so favoriting is a quick manual step in the app.
- Compliance checks fail toward `UNKNOWN` (fetch failure, unrecognized page
  structure, or disagreement between Musaffa and Zoya), not toward a false
  "compliant" — flagged for you to check manually rather than silently
  passed.
