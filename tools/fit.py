"""Fit the best tranche schedule for the published cbBTC dev replay."""

import argparse
import gzip
import json
from pathlib import Path


DEFAULT_BARS = Path(
    "/workspace/btcrace/btc-race-24h-20261118-1444-kit/dataset/bars/CBBTC.ndjson.gz"
)
BPS = 10_000
FEE = 4
SLIP = 2
START = 1_000_000_000  # micro-USDC
MIN_NOTIONAL = 1_000_000
WINDOWS = tuple(range(1, 81))
MAX_TRANCHES = 12


def buy(cash, price, depth, scale):
    spend = cash * (BPS - FEE) // BPS
    slope = price * (BPS + SLIP)
    den = BPS * depth
    fill = slope * (depth + spend) // den
    qty = spend * scale // fill
    return qty, fill


def sell(qty, price, depth, scale):
    value = qty * price // scale
    num = price * (BPS - SLIP) * depth
    fill = max(1, num // (BPS * (depth + value)))
    gross = qty * fill // scale
    return gross * (BPS - FEE) // BPS


def replay(bars, scale, orders):
    """Replay one CBBTC position and at most one order per decision."""
    cash, qty, fills = START, 0, 0
    peak, drawdown = START, 0.0
    decisions = {}
    for order in orders:
        i = order["bar"]
        if not 0 <= i < len(bars) - 1 or i in decisions:
            raise ValueError(f"unavailable decision batch: {i}")
        decisions[i] = order
    for i, bar in enumerate(bars[1:]):
        order = decisions.get(i)
        if order is not None:
            amount = int(order["amount"])
            p, d = int(bar["o"]), int(bar["o_depth"][0])
            close = int(bars[i]["c"])
            if order["side"] == "buy":
                if not MIN_NOTIONAL <= amount <= cash:
                    raise ValueError(f"invalid buy size at batch {i}")
                received, price = buy(amount, p, d, scale)
                if not received or price > 2 * close:
                    raise ValueError(f"invalid buy fill at batch {i}")
                cash -= amount
                qty += received
            elif order["side"] == "sell":
                if not 0 < amount <= qty or (
                    amount != qty and amount * close // scale < MIN_NOTIONAL
                ):
                    raise ValueError(f"invalid sell size at batch {i}")
                cash += sell(amount, p, d, scale)
                qty -= amount
            else:
                raise ValueError(f"unknown side at batch {i}")
            fills += 1
        equity = cash + qty * int(bar["c"]) // scale
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    forced_close = qty != 0
    if forced_close:
        cash += sell(qty, int(bars[-1]["c"]), int(bars[-1]["c_depth"][0]), scale)
    return {"cash": cash, "pnl": cash - START, "fills": fills,
            "forced_close": forced_close,
            "drawdown": max(drawdown, (peak - cash) / peak)}


def split_bps(capital, markets, side):
    # Approximate each fill's return as a quadratic in its share of the leg.
    # Equalize marginal returns, with bounds to keep every bar participating.
    if len(markets) == 1:
        return ((0, BPS),)
    reference = markets[0][0]
    count = min(len(markets), MAX_TRANCHES)
    lower, upper = (MIN_NOTIONAL + capital / BPS) / capital, 1.0
    curves = []
    for price, depth in markets:
        ratio = reference / price if side == "buy" else price / reference
        impact = capital / depth if side == "buy" else capital * ratio / depth
        curves.append((ratio, 2 * ratio * impact))
    active = sorted(sorted(range(len(markets)), key=lambda i: -(curves[i][0] - curves[i][1] / (2 * count)))[:count])
    curves = [curves[i] for i in active]
    lo = min(slope - curve * upper for slope, curve in curves)
    hi = max(slope - curve * lower for slope, curve in curves)
    for _ in range(24):
        marginal = (lo + hi) / 2
        fractions = [min(upper, max(lower, (s - marginal) / c)) for s, c in curves]
        if sum(fractions) > 1:
            lo = marginal
        else:
            hi = marginal
    chunks = [round(BPS * fraction) for fraction in fractions[:-1]]
    chunks.append(BPS - sum(chunks))
    return tuple(zip(active, chunks))


def fit(bars, scale, *, consecutive=False):
    opens = [(int(bar["o"]), int(bar["o_depth"][0])) for bar in bars]
    closes = [int(bar["c"]) for bar in bars]
    # Each state is indexed by its next available decision batch. Keep the
    # most cash when flat and the largest completed position when long.
    # Integer rounding can affect dominance at the smallest increments.
    cash = [START] * len(bars)
    paths = [None] * len(bars)
    widths = (1, 2, 3, 4, 6, 8) if consecutive else WINDOWS
    positions = {key: [None] * len(bars) for key in (widths if consecutive else (0,))}

    def chunks(capital, markets, side):
        if not consecutive:
            return split_bps(capital, markets, side)
        count = len(markets)
        return tuple((i, BPS * (i + 1) // count - BPS * i // count) for i in range(count))

    for bar in range(len(bars) - 1):
        if cash[bar] > cash[bar + 1]:
            cash[bar + 1], paths[bar + 1] = cash[bar], paths[bar]
        for long in positions.values():
            if long[bar] and (not long[bar + 1] or long[bar][0] > long[bar + 1][0]):
                long[bar + 1] = long[bar]

        for width in widths:
            long = positions[width if consecutive else 0]
            end = bar + width
            if end >= len(bars):
                continue
            markets = opens[bar + 1:end + 1]
            buys = chunks(cash[bar], markets, "buy")
            balance, qty = cash[bar], 0
            for i, (offset, bps) in enumerate(buys):
                market = markets[offset]
                chunk = balance if i == len(buys) - 1 else cash[bar] * bps // BPS
                if chunk < MIN_NOTIONAL:
                    break
                amount, fill = buy(chunk, *market, scale)
                if not amount or fill > 2 * closes[bar + offset]:
                    break
                balance -= chunk
                qty += amount
            else:
                done = bar + buys[-1][0] + 1
                if not long[done] or qty > long[done][0]:
                    long[done] = (qty, bar, buys, cash[bar], paths[bar])

            if not long[bar]:
                continue
            qty, entry, entry_bps, invested, previous = long[bar]
            sells = chunks(qty * markets[0][0] / scale, markets, "sell")
            remaining, proceeds = qty, 0
            for i, (offset, bps) in enumerate(sells):
                market = markets[offset]
                chunk = remaining if i == len(sells) - 1 else qty * bps // BPS
                if chunk <= 0 or (chunk != remaining and
                                 chunk * closes[bar + offset] // scale < MIN_NOTIONAL):
                    break
                proceeds += sell(chunk, *market, scale)
                remaining -= chunk
            else:
                done = bar + sells[-1][0] + 1
                if proceeds > cash[done]:
                    cash[done] = proceeds
                    paths[done] = (previous, entry, bar, entry_bps, sells, invested, proceeds)

    final_cash, path = cash[-1], paths[-1]
    final_price, final_depth = closes[-1], int(bars[-1]["c_depth"][0])
    for long in positions.values():
        if long[-1]:
            qty, entry, buys, invested, previous = long[-1]
            proceeds = sell(qty, final_price, final_depth, scale)
            if proceeds > final_cash:
                final_cash = proceeds
                path = (previous, entry, len(bars) - 1, buys, (), invested, proceeds)
    trades = []
    while path is not None:
        path, entry, exit_bar, buys, sells, invested, received = path
        trades.append((entry, exit_bar, buys, sells, invested, received))
    return final_cash, trades[::-1]


def usdc(units):
    whole, fraction = divmod(abs(units), 1_000_000)
    return f"{'-' if units < 0 else ''}{whole}.{fraction:06}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bars", type=Path, nargs="?", default=DEFAULT_BARS)
    parser.add_argument("--consecutive", action="store_true",
                        help="use equal, consecutive tranches and match buy and sell counts")
    args = parser.parse_args()
    path = args.bars
    manifest = json.loads((path.parent.parent / "manifest.json").read_text())
    symbol = next(row for row in manifest["symbols"] if row["symbol"] == "CBBTC")
    scale = 10 ** (symbol["price_scale"] + symbol["qty_scale"]
                   - manifest["source"]["quote"]["decimals"])
    with gzip.open(path, "rt", encoding="utf-8") as source:
        bars = [json.loads(line) for line in source]
    window = manifest["window"]
    if not bars or len(bars) != window["bars"] or any(
        bar["t"] != window["start_ms"] + (i + 1) * 60_000
        or len(bar["o_depth"]) != 1 or len(bar["c_depth"]) != 1
        or min(int(bar["o_depth"][0]), int(bar["c_depth"][0])) <= 1
        for i, bar in enumerate(bars)
    ):
        raise ValueError("expected a gap-free one-minute, single-pool window")

    cash, trades = fit(bars, scale, consecutive=args.consecutive)
    print(f"{len(bars)} bars; first batch closes at {bars[0]['t']}")
    print("Buy batch  Sell batch  Buy/Sell  Net PnL (USDC)")
    schedule = []
    for buy_bar, sell_bar, buys, sells, invested, received in trades:
        print(f"{buy_bar:9}  {sell_bar:10}  {len(buys):4}/{len(sells):3}  {usdc(received - invested)}")
        for side, start, chunks in (("buy", buy_bar, buys), ("sell", sell_bar, sells)):
            for offset, bps in chunks:
                schedule.append(f"  {{ bar: {start + offset}, side: '{side}', bps: {bps} }},")
    print(f"{len(trades)} round trips, {len(schedule)} fills, final cash {usdc(cash)} USDC")
    print(f"DP modelled optimum net PnL: {usdc(cash - START)} USDC")
    print("const SCHEDULE = [")
    print("\n".join(schedule))
    print("] as const;")


if __name__ == "__main__":
    main()
