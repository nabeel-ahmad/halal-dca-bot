# Context handoff — Halal DCA bot on Binance

Paste this into Claude Code (in the folder where you've placed these files) to continue.

## Goal
A semi-automated, near-zero-cost "propose, I approve" DCA bot that buys shariah-screened
US stocks through Binance's new stock-trading feature. Not fully autonomous by design —
it proposes trades on a schedule and waits for my Telegram approval before placing orders.

## Files already built (in this folder)
- `halal-investing-binance-guide.md` — full background: Binance stock feature specifics,
  the actual IFG shariah screening criteria (5-point Mufti Taqi Usmani test), architecture
  rationale, security requirements.
- `halal_dca_bot.py` — the bot itself. Reads `halal_universe.csv`, prices + balances from
  Binance, computes a rebalance-toward-target-weight proposal, sends it via Telegram, waits
  for "yes" before calling `place_stock_order()`. Spot-only, hard USD caps enforced locally.
- `halal_universe.csv` — my target watchlist (ticker, target_weight). **Currently has
  placeholder tickers (AAPL/MSFT/JNJ/COST) that need to be replaced with my real, verified list.**
- `halal-dca-bot-workflow.yml` — GitHub Actions cron workflow (monthly), meant to be moved to
  `.github/workflows/halal-dca-bot.yml` in a repo.
- `requirements.txt`, `.env.example` — dependencies + local env var template (no real secrets).

## Progress so far (done outside Claude Code)
1. ✅ Binance account created, KYC (Level 1 + 2) approved, Stocks tab confirmed visible/live
   for my country, 2FA enabled.
2. ✅ Halal watchlist process defined (Zoya/Islamicly screening + cross-check against
   Binance's tradable stock list). *Need to confirm whether the actual `halal_universe.csv`
   has been updated with real tickers yet, or if that's still pending.*

## Known open item — needs verification, don't guess
`halal_dca_bot.py` has `get_current_prices()`, `get_account_holdings()`, and
`place_stock_order()` deliberately left as `NotImplementedError` stubs. Binance's stock
trading API is new (launched mid-2026) and the exact REST endpoint/payload shape wasn't
confirmed in prior research. **Before writing real implementations, check
developers.binance.com's current API docs for the stocks product** rather than assuming
the crypto spot API shape applies. Test with tiny dollar amounts before trusting it.

## Remaining steps (this is where Claude Code picks up)
3. Create a private GitHub repo, add these files, move the workflow YAML into
   `.github/workflows/`, `git init` / push.
4. Create a Telegram bot via @BotFather, get the chat ID.
5. Generate a Binance API key scoped to spot-trading only (no withdrawals, IP-restricted
   if possible). Add `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID` as GitHub Actions encrypted secrets — never commit them.
6. Fill in the three stubbed functions using confirmed Binance API docs.
7. Dry-run (`DRY_RUN=true`) for a few cycles, inspect proposals, only then flip to live.

## Non-negotiable constraints to keep in mind
- No margin/futures/leverage — spot only, keeps it halal and lower-risk.
- No fully autonomous execution — human approval stays in the loop.
- Never log or print API keys/secrets/tokens.
- Not financial or legal advice; user should still consult a qualified adviser for real
  investment decisions.
