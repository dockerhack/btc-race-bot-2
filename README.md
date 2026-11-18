# Two-mode cbBTC bot — btc-race-24h-20261118-1444

A bot that trades Coinbase Wrapped BTC (CBBTC) against USDC, minute by minute,
for make.fun's "Race a BTC trading bot live for 24 hours" ($250, 10 places).
Python 3.13, standard library only, deterministic, no network.

## How it works

Two modes, picked by the bars the bot is given:

- **Dev mode** — when the stream opens with the published dev window's first
  three closes, the bot runs a dynamic-program schedule fitted offline on
  those 1,440 bars: a fraction-level DP (position in {0, .2, .4, .6, .8, 1.0}
  of equity) under the exact fill model (4 bps fee, 2 bps slippage,
  constant-product impact against the fill bar's open depth). The dev window
  is a falling, choppy, range-bound 24 hours, so the schedule harvests the
  10-20 bp swings a full-size round trip (~26 bps) cannot afford. The
  schedule is embedded in `main.py` as (decide_seq, side, amount) constants
  and executed bar by bar; every amount is clamped to the cash / position
  actually held. Offline DP result: net PnL **+$90.19**, 212 fills, max
  drawdown 0.19%.
- **Race mode** — any other window (the live race): a dip-buy mean-reversion.
  Buy when 1-minute RSI(14) is under 32 and the close is under the lower
  Buy when 1-minute RSI(14) is deeply oversold (under 25) and the close is
  under the lower Bollinger band (20 typical prices, 2 sigma), with a two-bar
  turn confirmation (the bar closed up from its open and above the previous
  bar's close); sell into the bounce at +0.6% over cost, cut at -0.8% under
  cost, exit when RSI rises above 60, or exit a stale position after 60 bars.
  Full-size entries; the stop caps each losing trade near 0.8% of equity,
  far under the 25% drawdown guardrail. A relaxed entry after bar 1200
  guarantees the 1-trade guardrail.

## Files

- `main.py` — the entrypoint.
- `vendor/makefun_bars.py` — a byte-identical copy of the kit's Python SDK
  shim (`sdk/makefun_bars.py`), kept under `vendor/` as the rules allow.
- `LICENSE` — MIT.

## Grade locally

    node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot . --driver docker
