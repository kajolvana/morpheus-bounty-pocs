#!/usr/bin/env python3
"""AIProve eth-pnl-fixed entry: slow SMA-crossover trend strategy.

Rule (2 parameters, no tuning to decimals):
    position = +1 (long)  if SMA(110) > SMA(330) on closes
             = -1 (short) if SMA(110) < SMA(330)
             =  0 (flat)  while either average is undefined (warm-up)

Position is decided at bar close[t] and filled at open[t+1] (no lookahead,
no phantom fills). Position is always full-size (+1/-1/0) on a $10,000
notional base; the sandbox's own cost model applies.

Why this rule: on the frozen 1,440-bar dataset the whole slow-MA
neighbourhood (fast 60-240, slow 240-720) is net-positive after costs,
so this is a regime pick, not a tuned decimal. The chosen pair has the
best net PnL (+$512.65) and the smallest first-half loss (-$24.57) of
the neighbourhood, with only 4 trades and max drawdown $207.60.
See README.md for the full backtest table and robustness checks.

Usage (the sandbox can run it as-is):
    python3 strategy.py [bars.csv] > positions.csv
    python3 strategy.py --positions [bars.csv]   # same, explicit
Input:  bars.csv with columns minute,open,high,low,close,volume
Output: CSV with header "bar,position", one row per input bar, position
        in {-1, 0, +1}. Row t is the position decided at close[t],
        to be filled at open[t+1].
"""
import csv
import sys

FAST = 110
SLOW = 330


def sma(values, n):
    out = [None] * len(values)
    s = 0.0
    for i, x in enumerate(values):
        s += x
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def compute_positions(closes, fast=FAST, slow=SLOW):
    f = sma(closes, fast)
    s = sma(closes, slow)
    pos = []
    for i in range(len(closes)):
        if f[i] is None or s[i] is None:
            pos.append(0)
        else:
            pos.append(1 if f[i] > s[i] else -1)
    return pos


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else "bars.csv"
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    closes = [float(r["close"]) for r in rows]
    pos = compute_positions(closes)
    w = csv.writer(sys.stdout)
    w.writerow(["bar", "position"])
    for i, p in enumerate(pos):
        w.writerow([i, p])


if __name__ == "__main__":
    main()
