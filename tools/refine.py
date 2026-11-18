"""Refine a single-position dev schedule; emit fixed orders as JSON."""

import argparse
from functools import lru_cache
import gzip
import json
from pathlib import Path
import re
import sys

from fit import BPS, DEFAULT_BARS, FEE, MIN_NOTIONAL, SLIP, START, buy, replay, sell

with gzip.open(DEFAULT_BARS, 'rt') as source:
    bars = [json.loads(line) for line in source]
scale = 10**7
markets = [(int(b['o']), int(b['o_depth'][0]), int(a['c']))
           for a, b in zip(bars, bars[1:])]
last = int(bars[-1]['c']), int(bars[-1]['c_depth'][0])
parser = argparse.ArgumentParser(description=__doc__)
seed = parser.add_mutually_exclusive_group(required=True)
seed.add_argument('--seed', type=Path, help='TypeScript order table')
seed.add_argument('--states', type=int, help='seed from the partial-position grid')
parser.add_argument('--passes', type=int, default=5, help='extra fine search passes')
args = parser.parse_args()
if args.seed:
    orders = [dict(bar=int(i), side=side, amount=a)
              for i, side, a in re.findall(r"bar: (\d+), side: '(\w+)', amount: '(\d+)'",
                                            args.seed.read_text())]
else:
    from inventory import fit_targets
    orders = fit_targets(bars, scale, args.states)['orders']
replay(bars, scale, orders)
path = []
qty = 0
for order in orders:
    i, a = order['bar'], int(order['amount'])
    qty += buy(a, *markets[i][:2], scale)[0] if order['side'] == 'buy' else -a
    path.append((i, qty))

@lru_cache(maxsize=100000)
def flow(i, old, new):
    p, d, close = markets[i]
    q = new - old
    if q == 0:
        return 0
    if q < 0:
        if new and (-q) * close // scale < MIN_NOTIONAL:
            return None
        return sell(-q, p, d, scale)
    # Invert the continuous formula to bracket the integer fill. The exact
    # quantity and decision tolerance are checked with the integer model.
    value = q * p * (BPS + SLIP) / (BPS * scale)
    if value >= d:
        return None
    guess = int(value / (1 - value / d) * BPS / (BPS - FEE))
    lo, hi = max(MIN_NOTIONAL, guess - 8), max(MIN_NOTIONAL, guess + 8)
    while lo > MIN_NOTIONAL and buy(lo - 1, p, d, scale)[0] >= q:
        lo = max(MIN_NOTIONAL, lo - 2 * (hi - lo + 1))
    while buy(hi, p, d, scale)[0] < q:
        if hi >= START * 2:
            return None
        hi += 2 * (hi - lo + 1)
    while lo < hi:
        mid = (lo + hi) // 2
        if buy(mid, p, d, scale)[0] >= q:
            hi = mid
        else:
            lo = mid + 1
    exact, price = buy(lo, p, d, scale)
    return -lo if exact == q and price <= 2 * close else None

def score(path, emit=False):
    cash, old, orders = START, 0, []
    for i, new in path:
        delta = flow(i, old, new)
        if delta is None:
            return -1
        cash += delta
        if cash < 0:
            return -1
        if emit and delta:
            orders.append(dict(bar=i, side='buy' if delta < 0 else 'sell',
                               amount=str(-delta if delta < 0 else old-new)))
        old = new
    return orders if emit else cash + sell(old, *last, scale)

best = score(path)
if best < 0:
    parser.error('the starting schedule could not be converted to inventory targets')
print('initial', best - START, file=sys.stderr)
for step in (0, 20000, 5000, 1000, 200, 20, 1, 0) + (200, 20, 1, 0) * args.passes:
    if not step:
        start = 0
        for end, (_, q) in enumerate(path):
            if q and end + 1 != len(path):
                continue
            segment = path[start:end + 1]
            lo = path[start - 1][0] if start else -1
            hi = path[end + 1][0] if end + 1 < len(path) else len(markets)
            winner = segment
            for shift in (-3, -2, -1, 1, 2, 3):
                if lo < segment[0][0] + shift and segment[-1][0] + shift < hi:
                    path[start:end + 1] = [(i + shift, q) for i, q in segment]
                    value = score(path)
                    if value > best:
                        best, winner = value, path[start:end + 1]
            path[start:end + 1] = winner
            start = end + 1
    for j, (i, q) in enumerate(path):
        if step:
            moves = [(i, q + step*k) for k in (-3,-2,-1,1,2,3) if q+step*k >= 0]
        else:
            lo = path[j-1][0] + 1 if j else 0
            hi = path[j+1][0] if j+1 < len(path) else len(markets)
            moves = [(t, q) for t in range(max(lo, i-3), min(hi, i+4)) if t != i]
        if q == 0 and step:
            continue
        winner = path[j]
        for move in moves:
            path[j] = move
            value = score(path)
            if value > best:
                best, winner = value, move
        path[j] = winner
    print('step', step, best - START, file=sys.stderr)
orders = score(path, True)
result = replay(bars, scale, orders)
assert result['cash'] == best
print(result, file=sys.stderr)
print(json.dumps(orders))
