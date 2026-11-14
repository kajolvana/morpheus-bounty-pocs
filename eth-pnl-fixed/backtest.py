#!/usr/bin/env python3
"""Local backtest harness for the AIProve eth-pnl-fixed bounty.

Cost model (documented assumption — the spec README gives no numbers,
so this uses a standard Binance-spot-like model and reports sensitivity):
  - Capital: $10,000 notional; position in {-1, 0, +1} (full-size long /
    flat / short). One unit = $10,000 notional.
  - Signal computed at bar close[t]; filled at open[t+1] (no lookahead,
    no phantom fills). Position held through bar t+1 earns the
    open->close move of bar t+1.
  - Cost per position change: fee 10 bps (taker) + slippage 5 bps = 15 bps
    on the notional changed. Flip +1 -> -1 changes $20,000 -> cost $30.
  - Liquidate at final bar close (cost charged).
  - Max drawdown from the equity curve (start $10,000).

Usage: python3 backtest.py [--cost-bps 15] [--split]
"""
import csv
import statistics
import sys

CAPITAL = 10000.0
COST_BPS = 15.0  # 10 fee + 5 slippage, per unit of position change


def load_bars(path="bars.csv"):
    rows = list(csv.DictReader(open(path)))
    return {
        "open": [float(r["open"]) for r in rows],
        "high": [float(r["high"]) for r in rows],
        "low": [float(r["low"]) for r in rows],
        "close": [float(r["close"]) for r in rows],
        "volume": [float(r["volume"]) for r in rows],
    }


def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out = [None] * len(xs)
    k = 2.0 / (n + 1)
    v = None
    for i, x in enumerate(xs):
        v = x if v is None else x * k + v * (1 - k)
        out[i] = v
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
        if i >= n:
            ag = sum(gains[-n:]) / n
            al = sum(losses[-n:]) / n
            if al == 0:
                out[i] = 100.0
            else:
                rs = ag / al
                out[i] = 100.0 - 100.0 / (1 + rs)
    return out


def signals_ma(bars, fast, slow):
    f = sma(bars["close"], fast)
    s = sma(bars["close"], slow)
    sig = [0] * len(bars["close"])
    for i in range(len(sig)):
        if f[i] is None or s[i] is None:
            sig[i] = 0
        else:
            sig[i] = 1 if f[i] > s[i] else -1
    return sig


def signals_momentum(bars, n):
    c = bars["close"]
    sig = [0] * len(c)
    for i in range(len(c)):
        if i < n:
            sig[i] = 0
        else:
            sig[i] = 1 if c[i] > c[i - n] else -1
    return sig


def signals_rsi(bars, n=14, hi=70, lo=30):
    r = rsi(bars["close"], n)
    sig = [0] * len(bars["close"])
    cur = 0
    for i in range(len(sig)):
        if r[i] is None:
            sig[i] = 0
            continue
        if r[i] > hi:
            cur = -1
        elif r[i] < lo:
            cur = 1
        sig[i] = cur
    return sig


def signals_donchian(bars, n):
    hi, lo, c = bars["high"], bars["low"], bars["close"]
    sig = [0] * len(c)
    cur = 0
    for i in range(len(c)):
        if i < n:
            sig[i] = 0
            continue
        if c[i] > max(hi[i - n:i]):
            cur = 1
        elif c[i] < min(lo[i - n:i]):
            cur = -1
        sig[i] = cur
    return sig


def signals_ema(bars, fast, slow):
    f = ema(bars["close"], fast)
    s = ema(bars["close"], slow)
    sig = [0] * len(bars["close"])
    for i in range(len(sig)):
        sig[i] = 1 if f[i] > s[i] else -1
    return sig


def signals_buyhold(bars):
    return [1] * len(bars["close"])


def backtest(bars, sig, cost_bps=COST_BPS):
    """Replay positions. sig[t] = desired position decided at close[t],
    filled at open[t+1]. Returns dict of stats + equity curve."""
    o, c = bars["open"], bars["close"]
    n = len(c)
    pos = 0
    equity = CAPITAL
    peak = CAPITAL
    max_dd = 0.0
    trades = 0
    costs = 0.0
    gross = 0.0
    eq_curve = [CAPITAL]
    for t in range(n - 1):
        target = sig[t]
        if target != pos:
            changed = abs(target - pos) * CAPITAL
            cost = changed * cost_bps / 10000.0
            costs += cost
            equity -= cost
            trades += 1
            pos = target
        # hold pos through bar t+1, earn open->close
        pnl = pos * (c[t + 1] - o[t + 1]) / o[t + 1] * CAPITAL
        gross += pnl
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
        eq_curve.append(equity)
    # liquidate at final close
    if pos != 0:
        cost = abs(pos) * CAPITAL * cost_bps / 10000.0
        costs += cost
        equity -= cost
        trades += 1
    net = equity - CAPITAL
    return {
        "net": net,
        "gross": gross,
        "costs": costs,
        "trades": trades,
        "max_dd": max_dd,
        "final_equity": equity,
        "eq_curve": eq_curve,
    }


def signals_price_vs_sma(bars, n, deadband=0.0):
    """Long if close > SMA(n) by deadband, short if below by deadband,
    else hold previous position."""
    s = sma(bars["close"], n)
    c = bars["close"]
    sig = [0] * len(c)
    cur = 0
    for i in range(len(c)):
        if s[i] is None:
            sig[i] = 0
            continue
        gap = (c[i] - s[i]) / s[i]
        if gap > deadband:
            cur = 1
        elif gap < -deadband:
            cur = -1
        sig[i] = cur
    return sig


def signals_ma_deadband(bars, fast, slow, deadband=0.001):
    f = sma(bars["close"], fast)
    s = sma(bars["close"], slow)
    sig = [0] * len(bars["close"])
    cur = 0
    for i in range(len(sig)):
        if f[i] is None or s[i] is None:
            sig[i] = 0
            continue
        gap = (f[i] - s[i]) / s[i]
        if gap > deadband:
            cur = 1
        elif gap < -deadband:
            cur = -1
        sig[i] = cur
    return sig


def run_all():
    bars = load_bars()
    n = len(bars["close"])
    half = n // 2
    cands = {
        "buy_hold": signals_buyhold(bars),
        "ma_10_60": signals_ma(bars, 10, 60),
        "ma_5_30": signals_ma(bars, 5, 30),
        "ma_10_30": signals_ma(bars, 10, 30),
        "ma_20_60": signals_ma(bars, 20, 60),
        "ma_20_120": signals_ma(bars, 20, 120),
        "ma_60_240": signals_ma(bars, 60, 240),
        "ma_120_360": signals_ma(bars, 120, 360),
        "ma_60_180": signals_ma(bars, 60, 180),
        "ma_120_240": signals_ma(bars, 120, 240),
        "ema_12_26": signals_ema(bars, 12, 26),
        "ema_10_30": signals_ema(bars, 10, 30),
        "mom_20": signals_momentum(bars, 20),
        "mom_30": signals_momentum(bars, 30),
        "mom_60": signals_momentum(bars, 60),
        "mom_120": signals_momentum(bars, 120),
        "rsi_14": signals_rsi(bars, 14, 70, 30),
        "donch_20": signals_donchian(bars, 20),
        "donch_40": signals_donchian(bars, 40),
        "donch_60": signals_donchian(bars, 60),
        "donch_120": signals_donchian(bars, 120),
        "p_vs_sma60": signals_price_vs_sma(bars, 60),
        "p_vs_sma120": signals_price_vs_sma(bars, 120),
        "p_vs_sma240": signals_price_vs_sma(bars, 240),
        "p_vs_sma120_db": signals_price_vs_sma(bars, 120, 0.002),
        "p_vs_sma240_db": signals_price_vs_sma(bars, 240, 0.002),
        "ma60_240_db": signals_ma_deadband(bars, 60, 240, 0.001),
        "ma20_120_db": signals_ma_deadband(bars, 20, 120, 0.001),
    }
    print(f"{'strategy':<12} {'net_full':>10} {'net_H1':>10} {'net_H2':>10} "
          f"{'maxDD':>9} {'trades':>7} {'costs':>8}")
    results = {}
    for name, sig in cands.items():
        full = backtest(bars, sig)
        h1 = backtest(
            {k: v[:half] for k, v in bars.items()}, sig[:half])
        h2 = backtest(
            {k: v[half:] for k, v in bars.items()}, sig[half:])
        results[name] = (full, h1, h2)
        print(f"{name:<12} {full['net']:>10.2f} {h1['net']:>10.2f} {h2['net']:>10.2f} "
              f"{full['max_dd']:>9.2f} {full['trades']:>7} {full['costs']:>8.2f}")
    return bars, results


def run_sensitivity(bars, results):
    print("\n--- cost sensitivity (net PnL at different cost levels) ---")
    names = ["ma_10_60", "ma_20_60", "mom_60", "donch_20", "ema_12_26",
             "buy_hold"]
    sigs = {
        "ma_10_60": signals_ma(bars, 10, 60),
        "ma_20_60": signals_ma(bars, 20, 60),
        "mom_60": signals_momentum(bars, 60),
        "donch_20": signals_donchian(bars, 20),
        "ema_12_26": signals_ema(bars, 12, 26),
        "buy_hold": signals_buyhold(bars),
    }
    print(f"{'strategy':<12}" + "".join(f"{cb:>9}bps" for cb in [0, 10, 15, 20, 30, 50]))
    for name in names:
        vals = [backtest(bars, sigs[name], cb)["net"] for cb in [0, 10, 15, 20, 30, 50]]
        print(f"{name:<12}" + "".join(f"{v:>12.2f}" for v in vals))


if __name__ == "__main__":
    cost = float(sys.argv[sys.argv.index("--cost-bps") + 1]) \
        if "--cost-bps" in sys.argv else COST_BPS
    print(f"cost model: {cost} bps per unit position change "
          f"(fee 10 + slippage 5 at default 15), capital ${CAPITAL:,.0f}")
    bars, results = run_all()
    run_sensitivity(bars, results)
