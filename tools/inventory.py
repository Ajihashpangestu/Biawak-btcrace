"""Offline partial-position DP; stdout is JSON, never writes files.

The relaxed search uses a fixed asset grid and concave cash dominance.
Divide-and-conquer monotone maximization avoids a quadratic transition scan.
Every candidate is subsequently checked by fit.py's integer replay.
"""
import argparse
from array import array
import gzip
import json
from pathlib import Path
try:
    from tools.fit import buy, sell
except ModuleNotFoundError:
    from fit import buy, sell

BPS = 10_000
FEE = 4
MIN_NOTIONAL = 1_000_000
START = 1_000_000_000


def optimize(bars, scale, states):
    # Fixed quantity grid, including room for the capital's appreciation.
    max_qty = int(1_150_000_000 * scale / min(int(b['o']) for b in bars))
    step = max_qty // states
    cash = [-1e100] * (states + 1)
    cash[0] = 1_000_000_000.0
    history = []
    for i in range(len(bars) - 1):
        bar = bars[i + 1]
        p, d = int(bar['o']), int(bar['o_depth'][0])
        unit = step * p / scale
        buy_unit = unit * 1.0002
        sell_unit = unit * 0.9998 * 0.9996
        result = [-1e100] * (states + 1)
        parents = array('H', [0]) * (states + 1)

        def search(lo, hi, left, right):
            if lo > hi:
                return
            mid = (lo + hi) // 2
            best, arg = -1e100, left
            for old in range(left, right + 1):
                delta = mid - old
                if delta > 0:
                    value = delta * buy_unit
                    candidate = cash[old] - value / (1 - value / d) / 0.9996
                else:
                    candidate = cash[old] + (-delta) * sell_unit / (1 + (-delta) * unit / d)
                if candidate > best:
                    best, arg = candidate, old
            # Negative cash cannot fund an order. Keep tiny tolerance only for floats.
            result[mid] = best if best >= -0.001 else -1e100
            parents[mid] = arg
            search(lo, mid - 1, left, arg)
            search(mid + 1, hi, arg, right)

        search(0, states, 0, states)
        cash = result
        history.append(parents)
        if i % 240 == 0:
            import sys
            print(f'grid {states}: bar {i}', file=sys.stderr, flush=True)
    p, d = int(bars[-1]['c']), int(bars[-1]['c_depth'][0])
    def final(j):
        v = j * step * p / scale
        return cash[j] + v * 0.9998 * 0.9996 / (1 + v / d)
    state = max(range(states + 1), key=final)
    optimum = final(state)
    schedule = []
    for i in range(len(history) - 1, -1, -1):
        previous = history[i][state]
        if previous != state:
            schedule.append({'bar': i, 'target': str(state * step)})
        state = previous
    return {'relaxed_cash': optimum, 'step': step, 'schedule': schedule[::-1]}


def fit_targets(bars, scale, states):
    """Turn the inventory path into min-notional executable one-order batches."""
    result = optimize(bars, scale, states)
    targets = {int(event['bar']): int(event['target']) for event in result['schedule']}
    cash, qty = START, 0
    orders, equity = [], []

    def mark(bar):
        return cash + qty * int(bar['c']) // scale

    peak = mark(bars[0])
    max_drawdown = 0
    equity.append(peak)
    for i in range(len(bars) - 1):
        target = targets.get(i, qty)
        bar = bars[i + 1]
        price, depth = int(bar['o']), int(bar['o_depth'][0])
        if target > qty:
            delta = target - qty
            value = delta * price // scale
            notional = (value * BPS + BPS - FEE - 1) // (BPS - FEE)
            while notional <= cash and buy(notional, price, depth, scale)[0] < delta:
                notional += 1
            amount, _fill = buy(notional, price, depth, scale)
            if MIN_NOTIONAL <= notional <= cash and amount >= delta:
                orders.append({'bar': i, 'side': 'buy', 'amount': str(notional)})
                cash -= notional
                qty += amount
        elif target < qty:
            delta = qty - target
            decision_close = int(bars[i]['c'])
            if delta == qty or delta * decision_close // scale >= MIN_NOTIONAL:
                orders.append({'bar': i, 'side': 'sell', 'amount': str(delta)})
                cash += sell(delta, price, depth, scale)
                qty -= delta
        equity.append(mark(bar))
        peak = max(peak, equity[-1])
        max_drawdown = max(max_drawdown, (peak - equity[-1]) / START)

    final_depth = int(bars[-1]['c_depth'][0])
    cash += sell(qty, int(bars[-1]['c']), final_depth, scale)
    max_drawdown = max(max_drawdown, (peak - cash) / START)
    result.update({
        'simulated_cash': cash,
        'simulated_pnl': cash - START,
        'fills': len(orders),
        'max_drawdown': max_drawdown,
        'orders': orders,
    })
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--states', type=int, default=6000)
    parser.add_argument('--partial', action='store_true', help='fit a partial-position path and exact executable orders')
    parser.add_argument('--bars', type=Path, default=Path('/workspace/btcrace/btc-race-24h-20261118-1444-kit/dataset/bars/CBBTC.ndjson.gz'))
    args = parser.parse_args()
    manifest = json.loads((args.bars.parent.parent / 'manifest.json').read_text())
    symbol = next(s for s in manifest['symbols'] if s['symbol'] == 'CBBTC')
    scale = 10 ** (symbol['price_scale'] + symbol['qty_scale'] - manifest['source']['quote']['decimals'])
    with gzip.open(args.bars, 'rt') as source:
        bars = [json.loads(line) for line in source]
    result = fit_targets(bars, scale, args.states) if args.partial else optimize(bars, scale, args.states)
    print(json.dumps(result))
