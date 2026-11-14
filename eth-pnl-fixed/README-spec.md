# Challenge: eth-backtest

A frozen ETH 1-minute OHLCV dataset (`bars.csv`, 1,440 bars) — real ETH/USDT
spot bars (Binance, 24h window ending 2026-11-13 12:17 UTC), not synthetic.
Every entry sees the exact same bars, so there is no data edge — this is a pure
strategy contest.

- Entry: a strategy that produces positions over the bars, plus the repo at one
  commit and the build transcript.
- Metric: **net PnL after costs** (maker + taker fees, slippage scaled by size).
  The sandbox replays your strategy with no phantom fills. Ties break on lower
  max drawdown.
- Overfitting to this sample is easy; a held-out tail punishes it.
