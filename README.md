# Halal Portfolio Weekly Review

This is a small helper that checks your Binance stock/ETF portfolio once a
week and emails you a report. It looks at whether your holdings are still
Sharia-compliant, whether you're too concentrated in any one stock, and
whether anything you own trips a couple of extra ethics checks — and rolls
those into a "Consider selling" list when a holding's compliance status,
ethics flags, or halal letter grade have gotten worse. It also suggests a few
new Sharia-compliant stocks and ETFs you could add. **It only reads your
account and sends an email — it never buys or sells anything for you.**

**This is not financial advice.** It just applies a fixed, mechanical set of
rules every week (a screener filter you picked, a cash target, some
exclusion lists) — it isn't making judgment calls, and the stocks/ETFs it
suggests aren't a recommendation to actually buy them.

## What's here

| File | Purpose |
|---|---|
| `weekly_portfolio_review.py` | Entry point. Re-checks current holdings' Sharia compliance (Musaffa + Zoya + optional Halal Terminal, checked in parallel) and halal letter grade, flags concentration risk and additional ethics screens, surfaces a "Consider selling" list, and emails a shortlist of A/A+-rated candidate stocks & ETFs matching a Musaffa screener filter, with a mechanical $ split of idle cash. |
| `binance_equity.py` | Binance Stocks Trading API client (signed requests, holdings, prices) + SMTP sender, shared by the review script. |
| `musaffa_recommendations.py` | Headless-browser (Playwright) scraper for the Musaffa screener and per-ticker Halal Rating grade — the screener table is JS-rendered, so a plain HTTP request won't see it. |
| `ethics_screens.py` | Small, manually-curated reference lists used to warn on existing holdings and exclude from new candidates — see inline comments in the file for sourcing. |
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
4. Optional — a third compliance source: sign up yourself at
   halalterminal.com for a free-tier API key, then add it as the
   `HALAL_TERMINAL_API_KEY` repo secret (and/or your local `.env`). Leave it
   unset and the review just runs on Musaffa + Zoya as before.

## Consider-selling list

Each week, every current holding is re-checked and flagged for the "Consider
selling" list if any of these have changed since you bought it:

- **Compliance**: it's no longer Sharia-compliant (one of the configured
  sources disagrees with its original COMPLIANT status).
- **Ethics**: it now trips one of the ethics screens below.
- **Halal rating**: its Musaffa letter grade has dropped below A (A- or
  lower).

This list is informational only — nothing is ever sold automatically; you
still make the call and sell manually in Binance if you agree.

Analyst rating is **not** part of this list for existing holdings: Musaffa
only exposes an analyst-consensus rating through its screener results, which
only covers a filtered subset of tickers, not an arbitrary ticker you already
hold. It's still used (and shown) for this week's new-money candidates, since
those come directly from the screener.

## Ethics screens

`ethics_screens.py` holds short, manually-curated reference lists — not large
structured databases, since no clean API publishes this data. Sourcing and
scope notes live as inline comments in that file rather than here. Re-verify
against the source pages periodically; a stale list under- or over-flags
silently otherwise.

## Safety

- Read-only. No trade is ever executed by this project.
- Binance has no API for marking watchlist favorites — the weekly email
  lists tickers so favoriting is a quick manual step in the app.
- Compliance checks fail toward `UNKNOWN` (fetch failure, unrecognized page
  structure, or disagreement between sources), not toward a false
  "compliant" — flagged for you to check manually rather than silently
  passed.
