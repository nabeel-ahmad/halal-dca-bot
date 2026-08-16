---
name: halal-portfolio-review
description: Run, debug, or extend the weekly halal portfolio review (weekly_portfolio_review.py) — Sharia compliance re-check, concentration warnings, ethics screens, Musaffa candidate recommendations. Use when asked to run the weekly review, diagnose a failed or flaky run, add/update an ethics screen list, or explain why a ticker was flagged or excluded from recommendations.
---

# Halal portfolio review

Read-only pipeline: re-checks current Binance equity holdings for Sharia
compliance and halal letter grade, flags concentration risk and other
ethics-screen exposure, rolls worsened holdings into a "Consider selling"
list, and emails a shortlist of A/A+-rated candidate stocks & ETFs. It never
places an order.
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
   `HALAL_TERMINAL_API_KEY` is optional — its check is skipped, not
   `UNKNOWN`-flagged, when unset.
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
  `weekly_portfolio_review.py` requires every configured source to agree on
  `COMPLIANT` — any fetch failure, page-structure mismatch, or disagreement
  between sources surfaces as `UNKNOWN` by design (fail toward "check this
  yourself," never toward a false-positive "compliant"). Sources run in
  parallel via a `ThreadPoolExecutor`, so one slow source doesn't add to the
  others' latency.
- **Halal Terminal always shows `UNKNOWN` / "not set — skipped"**:
  `HALAL_TERMINAL_API_KEY` isn't set in this environment, so
  `check_compliance` doesn't include it at all (see `if
  HALAL_TERMINAL_API_KEY:` in `check_compliance`) — the row you see is
  `check_halal_terminal`'s own early return, not a real API failure. A 401
  means the key itself is wrong; 429 means the free-tier quota (token-based,
  5 tokens per screen) ran out for the period.
- **A holding is missing from "Consider selling" despite a bad grade, or
  vice versa**: `grade_below_a` in `musaffa_recommendations.py` treats
  `UNKNOWN` as not-a-downgrade on purpose — a scrape timeout isn't a real
  signal. `get_halal_grades` doesn't know if a held ticker is a stock or an
  ETF (Binance's holdings response doesn't say), so it tries
  `musaffa.com/stock/<TICKER>` first and only falls back to `/etf/<TICKER>`
  on an `UNKNOWN` result — if that ordering ever picks the wrong page for a
  ticker that coincidentally resolves on both paths, fix the fallback logic
  there.

## Updating the ethics-screen lists

`ethics_screens.py` holds short, hand-curated reference lists — not a scraped
or API-backed feed, since no clean source publishes one. Sourcing and scope
notes for each list live as inline comments in that file. To add or correct
an entry: verify the company and its ticker independently before adding — a
wrong ticker either clears a real flagged target or falsely flags an
unrelated one, and both are worse than leaving a ticker unlisted. Re-check
each list against its cited source periodically, since the sources are prose
pages, not scrapable tables.

Do not build a screen based on inferred political-lobby affiliation — there's
no reliable public database for that, and a hand-curated guess would be an
unsupported claim about a real company, not a sourced fact like the lists
above.
