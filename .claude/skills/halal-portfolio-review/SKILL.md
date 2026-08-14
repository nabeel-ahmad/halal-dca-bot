---
name: halal-portfolio-review
description: Run, debug, or extend the weekly halal portfolio review (weekly_portfolio_review.py) — Sharia compliance re-check, concentration warnings, BDS/Pentagon ethics flags, Musaffa candidate recommendations. Use when asked to run the weekly review, diagnose a failed or flaky run, add/update the BDS or DoD-contractor screen, or explain why a ticker was flagged or excluded from recommendations.
---

# Halal portfolio review

Read-only pipeline: re-checks current Binance equity holdings for Sharia
compliance, flags concentration and BDS/Pentagon exposure, and emails a
shortlist of A/A+-rated candidate stocks & ETFs. It never places an order.
Full architecture is in [README.md](../../../README.md) — read that first for
file layout and the self-hosted-runner requirement; this skill covers the
parts that aren't already legible from the code.

## Running it

1. Confirm the venv exists and has the browser installed:
   `/Users/hk-nabeel/.venvs/halal-dca-bot/bin/python -c "import playwright"` —
   if this fails, `pip install -r requirements.txt && playwright install
   chromium` in that venv first.
2. Confirm required env vars are set (`BINANCE_API_KEY`, `BINANCE_API_SECRET`,
   `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` — see
   `.env.example`). Locally, source a `.env`; in CI these are repo secrets.
3. Run `python weekly_portfolio_review.py` directly for a full live run
   (sends a real email), or import individual functions in a REPL to test
   one piece — `compute_recommendations`, `check_compliance`,
   `check_ethics_flags` all run standalone against live data.
4. To trigger the scheduled GitHub Actions run manually:
   `gh workflow run weekly-portfolio-review.yml`, then
   `gh run list --workflow=weekly-portfolio-review.yml --limit 1` for the
   run ID and `gh run view <id> --log` for output. The runner must be a
   self-hosted Mac with the runner service up — Binance 451s any
   GitHub-hosted-runner IP.

Done when the email arrives (or, for a REPL test, the function returns
without raising) — not when the script merely exits 0, since some failure
paths still print a note and continue.

## Diagnosing a flaky or wrong result

- **A pick's `halal_grade` is `UNKNOWN`**: this is the single most flaky part
  of the pipeline. Musaffa's per-ticker page renders the "Screening
  Methodology" label before the grade itself arrives via a separate async
  call — `enrich_with_halal_grade` in `musaffa_recommendations.py` polls for
  up to 20s per ticker, but the site is occasionally slower than that. An
  `UNKNOWN` grade is a scrape timeout, not a real "no grade" signal — the
  email links each one to `musaffa.com/stock/<TICKER>` (or `/etf/<TICKER>`)
  to check by hand. Raise `range(20)` in that function if this fires often.
- **Candidate list looks empty or wrong**: the discovery filter is a literal
  URL (`STOCK_SCREENER_URL` / `ETF_SCREENER_URL` in
  `musaffa_recommendations.py`), not a live UI interaction — if Musaffa
  changes their filter param names or screener frontend, re-derive the URL
  by opening the screener in a browser, applying the filters through the UI,
  and copying the resulting address bar URL.
- **A holding's compliance shows `UNKNOWN`**: `check_compliance` in
  `weekly_portfolio_review.py` requires both Musaffa and Zoya to agree on
  `COMPLIANT` — any fetch failure, page-structure mismatch, or disagreement
  between the two surfaces as `UNKNOWN` by design (fail toward "check this
  yourself," never toward a false-positive "compliant").

## Updating the BDS / Pentagon-contractor screens

`ethics_screens.py` holds two short, hand-curated lists (`BDS_TARGETS`,
`DOD_CONTRACTORS`) — not a scraped or API-backed feed, since neither source
publishes one. To add or correct an entry: verify the company and its ticker
independently before adding — a wrong ticker either clears a real boycott
target or falsely flags an unrelated one, and both are worse than leaving a
ticker unlisted. Re-check `BDS_TARGETS` against
https://bdsmovement.net/get-involved/what-to-boycott periodically; it's prose
on a webpage, not a table, so this is a manual read, not a scrape.

Do not build an AIPAC-affiliation screen the same way — there's no reliable
public database mapping companies to political-lobby affiliation, and a
hand-curated guess here would be an unsupported claim about a real company,
not a sourced fact like the two lists above.
