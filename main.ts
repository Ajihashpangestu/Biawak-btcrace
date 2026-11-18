import { BPS, runBars, type Bar, type BarsState, type BuyInput, type SellInput } from './vendor/makefun-bars.ts';

const FAST = 8;
const SLOW = 21;
const ROC = 12;
const ATR = 14;
const ENTRY_GAP_BPS = 18;
const EXIT_GAP_BPS = 8;
const TRAIL_BPS = 85;
const MAX_SLIPPAGE_BPS = 250;

interface Series {
  closes: bigint[];
  fast: bigint | null;
  slow: bigint | null;
  prevClose: bigint | null;
  tr: bigint[];
  peak: bigint;
}
const series = new Map<string, Series>();

function getSeries(symbol: string): Series {
  let s = series.get(symbol);
  if (s === undefined) {
    s = { closes: [], fast: null, slow: null, prevClose: null, tr: [], peak: 0n };
    series.set(symbol, s);
  }
  return s;
}

function update(bar: Bar): Series {
  const s = getSeries(bar.symbol);
  const c = BigInt(bar.c);
  const h = BigInt(bar.h);
  const l = BigInt(bar.l);
  if (s.prevClose !== null) {
    let range = h - l;
    const up = h > s.prevClose ? h - s.prevClose : s.prevClose - h;
    const down = l > s.prevClose ? l - s.prevClose : s.prevClose - l;
    if (up > range) range = up;
    if (down > range) range = down;
    s.tr.push(range);
    if (s.tr.length > ATR) s.tr.shift();
  }
  s.prevClose = c;
  s.closes.push(c);
  if (s.closes.length > SLOW + ROC + 2) s.closes.shift();
  if (s.fast === null) {
    if (s.closes.length >= FAST) s.fast = s.closes.slice(-FAST).reduce((a, x) => a + x, 0n) / BigInt(FAST);
  } else s.fast = (s.fast * BigInt(FAST - 1) + c) / BigInt(FAST);
  if (s.slow === null) {
    if (s.closes.length >= SLOW) s.slow = s.closes.slice(-SLOW).reduce((a, x) => a + x, 0n) / BigInt(SLOW);
  } else s.slow = (s.slow * BigInt(SLOW - 1) + c) / BigInt(SLOW);
  if (c > s.peak) s.peak = c;
  return s;
}

function buyOrder(state: BarsState, symbol: string): BuyInput | null {
  const depth = state.depthAt(symbol);
  if (depth === null) return null;
  const room = (BigInt(state.hello.caps.max_weight_bps) * state.equity) / BPS;
  const impact = state.impactBps(symbol, room) ?? 0n;
  const capped = impact > 35n ? (room * 35n) / impact : room;
  const amount = capped < state.cash ? capped : state.cash;
  if (amount < BigInt(state.hello.fees.min_notional_quote_units)) return null;
  return { side: 'buy', symbol, notional_quote_units: amount, max_slippage_bps: MAX_SLIPPAGE_BPS };
}

await runBars({
  onBatch(state, batch) {
    const seen = new Set<string>();
    for (const bar of batch.bars) {
      seen.add(bar.symbol);
      update(bar);
    }
    if (state.warmingUp) return [];
    const symbol = state.hello.symbols[0]?.symbol ?? 'CBBTC';
    if (!seen.has(symbol)) return [];
    const s = series.get(symbol)!;
    const close = state.close(symbol);
    if (close === null || s.fast === null || s.slow === null || s.closes.length <= ROC) return [];
    const held = state.positions.get(symbol);
    const atr = s.tr.length === 0 ? 0n : s.tr.reduce((a, x) => a + x, 0n) / BigInt(s.tr.length);
    const gap = s.fast > s.slow ? s.fast - s.slow : 0n;
    const gapBps = s.slow === 0n ? 0n : (gap * BPS) / s.slow;
    const momentum = close > s.closes[s.closes.length - 1 - ROC]!;
    if (held !== undefined) {
      const cost = held.cost;
      const value = state.value(symbol, held.qty);
      const trail = s.peak > 0n && close * BPS < s.peak * BigInt(BPS - TRAIL_BPS);
      const weak = !momentum && gapBps <= BigInt(EXIT_GAP_BPS) && close + atr / 2n < s.slow;
      const loss = value * 995n < cost * 1000n;
      if (trail || weak || loss) return [{ side: 'sell', symbol, qty: held.qty, max_slippage_bps: MAX_SLIPPAGE_BPS } satisfies SellInput];
      return [];
    }
    if (momentum && gapBps >= BigInt(ENTRY_GAP_BPS) && close > s.slow + atr / 3n) {
      const order = buyOrder(state, symbol);
      return order === null ? [] : [order];
    }
    return [];
  },
});
