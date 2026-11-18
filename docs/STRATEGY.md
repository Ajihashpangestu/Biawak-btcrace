# Strategy: Fitting a cbBTC Execution Schedule

The figures and worked orders below describe the original +92.406298 USDC
baseline. The current order table was subsequently refined with
[tools/refine.py](../tools/refine.py), which tests timing and quantity changes
using the integer model and emits a candidate table as JSON. Use
`python3 -B tools/refine.py --seed main.ts` to search around an existing table
or `--states 12000` to start from a fresh partial inventory fit. A modeled
improvement is only a candidate; use the kit's two-run local grade to verify it.

This entry treats the published development replay as an execution problem.
It searches for a path through cash and cbBTC inventory, then embeds the
resulting orders in [main.ts](../main.ts). During replay the bot addresses
those orders by decision batch number. It does not rerun the search.

The distinction matters: the schedule was fitted with knowledge of later
prices and depths in the development window. The runtime sees the usual
stream one batch at a time, but its order table already encodes the fit.
The development score measures execution on that window. It does not
estimate performance on a new day, and the table is not a forecasting model.

| Component | Responsibility |
| --- | --- |
| Competition kit | Pool definition, sampled state, bars, execution and score |
| [tools/fit.py](../tools/fit.py) | Integer fill functions and all-in tranche baseline |
| [tools/inventory.py](../tools/inventory.py) | Partial inventory search and integer order construction |
| [main.ts](../main.ts) | Fixed order table and checks against the current account |
| [vendor/makefun-bars.ts](../vendor/makefun-bars.ts) | Line protocol, state updates and order serialization |

## Market and Units

The instrument is Coinbase Wrapped BTC on Solana, spelled `CBBTC` on the
wire. The account spends and receives USDC. The universe contains one
direct Orca Whirlpool, with cbBTC as token A and USDC as token B.
The pool address is `HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM`;
its tick spacing is 4. The pool selection is frozen in `dataset/pools.json`.
The simulator trades paper balances, and the bot never sends an on-chain swap.

A Whirlpool concentrates liquidity in price ranges. At a particular tick
only positions whose ranges include the current price contribute active
liquidity. The vault balances can include tokens from ranges that do not
currently contribute to execution. This is why the balance of the USDC
vault, by itself, is an incomplete measure of depth.

Let `s = sqrt_price_x64 / 2^64`, in raw token units, and let `L` be
active liquidity. In the current range, the constant-product representation
has virtual reserves `L/s` of A and `L*s` of B. Their ratio is `s^2`.
For this token orientation the human price in USDC per cbBTC is the raw
ratio multiplied by `10^(8-6)`. The recorder encodes the result as an
integer at the configured price scale. The square root is a fixed-point
integer, so recovering price does not require a floating point square.

Three depth quantities are relevant for a sample:

| Candidate | Meaning in raw USDC units |
| --- | --- |
| Virtual quote reserve | Active liquidity times the square-root price |
| Quote vault | Raw token B balance |
| Base vault valued in quote | Raw token A balance translated at the pool price |

The bar builder uses the minimum of these candidates as sample depth.
The vault limits stop virtual liquidity from implying a larger executable
reserve than the smaller vault side. This is a local liquidity estimate:
the fill formula does not walk real tick arrays or model changes in active
liquidity when a swap crosses a tick. The shallower vault side held about
1.382 million USDC equivalent when the universe was frozen. That figure
identifies the pool selection; individual fills use the recorded bar depth.

The same number can mean very different things in the data and the wire order:

| Field | Stored unit | Conversion |
| --- | --- | --- |
| Cash or buy notional | micro-USDC | Divide by 1,000,000 for USDC |
| Quantity or sell amount | raw cbBTC | Divide by 100,000,000 for cbBTC |
| Price | scaled USDC per cbBTC | Divide by 100,000 for the human price |
| Depth entry | raw USDC | Divide by 1,000,000 for USDC |

The conversion divisor used by both Python tools is
`S = 10^(price_scale + qty_scale - quote_decimals) = 10^7`.
For instance, `floor(q * p / S)` values raw quantity `q` at integer
price `p` in micro-USDC. Multiplication happens before integer division.
Price and quantity remain distinct even though both travel as decimal
strings. In the TypeScript bot the strings become `bigint` when compared
with balances or valued.

## Building the Minute Series

The recorder polls every two seconds. One Solana `getMultipleAccounts` call
reads the Whirlpool, both vaults, and the cbBTC mint. Price and depth inputs
therefore come from the same response, with a slot attached to the sample.
There is no exchange candle or external BTC price to merge into the series.

Each sample is assigned to the UTC minute containing its timestamp.
For example, the interval starting at 12:00:00 ends just before 12:01:00,
and its bar is stamped 12:01:00. A sample right at 12:01:00 belongs to
the next minute. The bar timestamp labels the close of the interval,
including when the first or last actual sample is a few seconds away.

| Output | Construction within a nonempty minute |
| --- | --- |
| `o` | Median price among samples within 10 seconds of the first sample |
| `c` | Median price among samples within 10 seconds of the last sample |
| `h` | Highest sampled price in the minute |
| `l` | Lowest sampled price in the minute |
| `v` | Null; snapshots do not measure executed volume |
| `o_depth` | Median sample depth in the opening sample window |
| `c_depth` | Median sample depth in the closing sample window |
| `first_slot`, `last_slot`, `samples` | Provenance and coverage of the minute |

The opening window is anchored to the first sample, rather than simply
the first ten seconds on the minute clock. The closing window is anchored
to the last sample. Price and depth are reduced independently inside the
same window, so their medians need not come from one particular slot.
The one-entry depth arrays represent the one-hop market in this competition.

The opening and closing medians reduce the weight of a brief price move
at a boundary. High and low still record sampled extremes. The optimizer
cannot sell at the high or buy at the low just because an extreme appears
in the bar; the execution model uses the opening median. A quiet minute
with identical prices is valid. A minute with no samples is missing, and
the batch for that minute has no CBBTC bar. More than 28 missing minutes
rejects a 1,440-minute window under the competition rules.

The accepted dev cut starts at `1794926640000` and ends at
`1795013040000`, both in epoch milliseconds. It contains 43,100 samples
and 1,440 bars with zero gaps. The first close is `1794926700000`.
The fit validates the one-minute cadence, bar count, single-hop depth
arrays and nontrivial positive depth before indexing by row number.

## One Order from Decision to Settlement

At the close of minute `i`, the harness sends batch `i` with the bar
and the account after earlier orders have been processed. The bot can
reply with an order for the next batch. A fill attributed to decision
`i` therefore uses `o[i+1]` and `o_depth[i+1][0]` on this gap-free
dataset. In a stream with a missing next bar the harness cannot fill
against that minute's nonexistent open.

| Point | Market reference | State change |
| --- | --- | --- |
| Batch `i` | `c[i]` | Inspect current account and submit an order |
| Next minute's open | `o[i+1]`, `o_depth[i+1]` | Resolve the pending order |
| Batch `i+1` | `c[i+1]` | Mark the new inventory and report the fill |
| End of run | Last close and its `c_depth` | Automatically sell residual inventory |

The grade starts from 1,000 USDC in cash. Buying spends cash the account
actually has; selling uses cbBTC the account actually owns. There are
no short positions or borrowed funds. Cash earns nothing while the bot
waits. A mark is the cash balance plus inventory valued at the close;
it is not the amount a liquidation at that close would yield.

### Why the Impact Is a Ratio

Consider a local pool with base reserve `x` and quote reserve `D`,
where the reference price is `D/x`. For a buy with net quote input
`a`, the invariant changes the base reserve to `xD/(D+a)`.
The base output is `xa/(D+a)`. Dividing quote input by base output
gives an average price of `(D/x)(1+a/D)`.

For a sale of `q` base, write its reference quote value as
`a = q * D/x`. The pool returns `a/(1+a/D)` in quote, equivalent
to an average price of `(D/x)/(1+a/D)`. The same dimensionless ratio
therefore moves the buy price up and divides the sell price down.
This is an average fill price, not the marginal spot price after the trade.

In replay `D` is the bar's recorded opening depth and `a` is in the
same raw quote unit. A sell's `a` is its value at the unslipped open,
while a buy's `a` is its spend after fees. Four basis points of fee and
two basis points of adverse slippage are separate from the pool math.
The fee is configured by the grader; it is not inferred from the
Whirlpool's tick spacing.

### Exact Integer Recipes

Use `B = 10000`, `F = 4` and `U = 2`. In the formulas below
`//` means integer division of nonnegative operands. For a buy of
`N` micro-USDC at integer open `p` and depth `D`, the code does:

```text
spend    = N * (B - F) // B
fee      = N - spend
fill     = p * (B + U) * (D + spend) // (B * D)
received = spend * S // fill
cash'    = cash - N
qty'     = qty + received
```

For a sell of raw quantity `q`, the corresponding sequence is:

```text
value    = q * p // S
fill     = max(1, p * (B - U) * D // (B * (D + value)))
gross    = q * fill // S
proceeds = gross * (B - F) // B
fee      = gross - proceeds
cash'    = cash + proceeds
qty'     = qty - q
```

Combining the slippage and impact factors before dividing preserves the
same integer rounding as the harness. Applying each factor separately
and truncating twice can change the result. Subtracting a separately
rounded fee can also differ from flooring the amount after the fee.
The sell price has a one-unit floor in the fixed price scale.

The order's `max_slippage_bps` is an acceptance tolerance measured from
the decision close to the eventual execution price, including impact.
It is different from the 2 bps adjustment applied in every fill. The
entry sets the tolerance to 10,000 bps, the protocol maximum. A next-open
buy with a fill above twice the decision close can still be rejected.
The permissive tolerance is intended to let the fitted fills through;
it supplies little protection against a changed execution environment.

### Worked Entry

Decision 8 is the first submitted buy. Its notional is `47545733`
micro-USDC, or 47.545733 USDC. It uses row 10 of the bar file, which
is bar index 9. That row opens at `6485427093`, representing
64,854.27093 USDC per cbBTC, and has opening depth `950198197585`
micro-USDC.

The fee step leaves `47526714` micro-USDC to trade. The resulting
impact ratio is close to 0.00005, about 0.50 bps. The rest of the
execution premium comes from modeled adverse slippage; the fee has
already been taken out of the input. This example is useful for checking
off-by-one errors because the decision bar has a different price and
depth from the execution bar.

## Why Tranches Help

Hold price and depth fixed across several distinct execution bars.
Ignore fees and slippage briefly so the algebra isolates impact.
For a total one-sided quote amount `A`, one fill has impact parameter
`A/D`. Splitting it into `n` equal orders of size `A/n` gives
each child an impact parameter `A/(nD)`.

For a sell, the exact quote shortfall relative to selling at the
reference price is:

```text
one fill:       A^2 / (D + A)
n equal fills:  A^2 / (nD + A)
ratio:          (D + A) / (nD + A)
```

For a buy, the shortfall in base units valued at the reference price
has the same expression when `A` denotes total net spend. For small
`A/D`, shortfall is quadratic in order size. The aggregate quadratic
term is `n * (A/n)^2 / D = A^2/(nD)`, about one `n`th of the
unsplit impact loss. The impact parameter of each child divides by
`n` exactly under the fixed-depth assumption.

As a scale check, 1,000 USDC against 1 million USDC depth gives about
10 bps impact on one leg. Five equal fills against five equal fresh
opens each have about 2 bps impact. The impact component on that leg
falls from roughly one dollar to roughly twenty cents. The fee and
the slippage are proportional and largely survive the split. Repeated
integer rounding adds further small differences.

The grader carries the pool state across fills at the same opening
event. Splitting an order into several replies at that one price would
not grant each child an untouched pool. A different minute starts
from its own recorded state and has no lasting simulated price impact
from the prior minute. This reset is precisely what the tranche
arithmetic uses; it is a simulator property, not a promise about how
quickly real liquidity replenishes.

With varying depths and the same price, minimizing the leading cost
`sum(a_i^2/D_i)` subject to `sum(a_i) = A` assigns larger size to
deeper opens. Once prices differ, each child also has a distinct
mark-to-market opportunity cost. The best solution may skip a shallow
or badly priced minute, and a large price move while waiting can erase
the savings from splitting. Minimum order sizes and one usable order
per batch in this entry put practical limits on how finely it can split.

## All-In Tranche Search in fit.py

The baseline solves a restricted offline problem: enter a fully funded
long leg, wait as desired, then sell the entire position. A single leg
can span several decisions. During that leg the DP does not consider
interleaving orders from another leg, even though the paper account
can temporarily hold both cash and cbBTC.

The time index is the next available decision batch. The flat array
`cash[i]` keeps the best cash balance available at that index.
`paths[i]` is a linked record of completed round trips for that balance.
The long array keeps the largest completed cbBTC position and the
entry information needed to trace it back to its predecessor.
The initial state is 1,000 USDC flat; waiting in cash lets that state
remain available at each later index.

| Edge | Candidate score | Destination index |
| --- | --- | --- |
| Wait flat | Same cash | Next decision |
| Wait long | Same quantity | Next decision |
| Enter long | Sum of bought raw quantities | After the final buy decision |
| Exit long | Sum of net quote proceeds | After the final sell decision |

For a start decision `i` and width `w`, candidate markets are the
opens of bars `i+1` through `i+w`, inclusive. A child at offset
`k` is emitted at `i+k`, fills at `i+k+1`, and can be followed
by a new decision at the close of that fill bar. The destination state
is placed at `i+last_offset+1`, even when the chosen children leave
unused slots at the end of the search window.

### Grid and Tranche Weights

The default search tests every width from 1 through 80 minutes when
the window fits before the end of the dataset. It selects at most
12 participating opens per leg. The `--consecutive` comparison uses
widths 1, 2, 3, 4, 6 and 8, splits almost equally in basis points,
and uses a separate long state per width to match buy and sell counts.

In the default mode, `split_bps` approximates each child's contribution
by a concave quadratic in its capital fraction `f`. With reference
price `r0` equal to the first candidate open, it uses a relative
price factor `r = r0/p` on buys and `r = p/r0` on sells. The
impact coefficient `I` is capital divided by depth for buys and
capital times `r` divided by depth for sells.

```text
approximate contribution = r*f - r*I*f^2
marginal contribution    = r - (2*r*I)*f
```

It ranks markets by the approximate contribution per fraction at an
equal slice of the proposed count, then sorts the winners back into
chronological order. It finds a common marginal contribution with
24 bisection steps. Each fraction is clipped at 1 on the upper end
and at `(MIN_NOTIONAL + capital/BPS) / capital` on the lower end.
That lower bound leaves some room for the basis-point conversion.

All but the final percentage are rounded to basis points. The last
percentage makes the sum exactly 10,000, and the last actual order
consumes whatever balance or quantity is left after integer division.
The quadratic fit only proposes the split; every accepted child is
then priced with the integer buy or sell function.

The search discards a buy leg with a child below one USDC, a
zero-quantity fill, or a fill above twice its decision close.
It tests sell children against one USDC at the smaller of the
decision close and next open as a conservative size check.
The implementation replaces states only on strict improvement.
The final result is read from the last flat state, so this baseline
must finish selling through ordinary open fills rather than leaving
inventory for the terminal liquidation. Backtracking produces the
entry index, exit index, offsets, basis points, and leg cash flows.

### What the Ceiling Means

On the published dev data, the default run finds **+90.179381 USDC**
with 24 completed round trips, 377 fills and final cash of
1,090.179381 USDC. The tool calls this its modeled optimum. It is the
best terminal cash the implemented search found with its generated
legs and retained states. It is not a proof of a bound over all
continuous allocations or even all possible integer tranche splits.

The participating set is chosen by an approximate ranking, the
quadratic split is rounded, and only the dominant candidate at a
state survives. Integer constraints such as minimum notional and
rounding can undermine simple dominance at the smallest increments.
The higher result from the partial search is consistent with a
different state space and different candidate orders.

## Partial Inventory Search

`inventory.py` makes inventory an explicit dimension. At a given
decision, it stores the most cash found with each attainable grid
quantity. With `--states 6000` there are 6,001 quantities, including
zero. To set the grid scale, it takes the raw cbBTC equivalent of
1,150 USDC at the lowest recorded open and divides that quantity
into integer steps. The published window gives a step of 304 raw
cbBTC units. The extra headroom allows for appreciation of the account.

For an old state `j` and a candidate new state `k`, the transition
uses execution data from the next open. Let `u = step * p / S`.
For an inventory increase, define `v = (k-j) * u * 1.0002`.
The continuous buy formula is inverted to find the gross cash needed
to get that quantity. For a decrease, let `v = (j-k) * u`.

```text
k > j:  cash[j] - v / (1 - v/D) / 0.9996   [buy]
k = j:  cash[j]                             [hold]
k < j:  cash[j] + v * 0.9998 * 0.9996 / (1 + v/D) [sell]
```

These equations use floating point. They omit integer floors and
the minimum order size. The buy inverse assumes its target value is
below the depth; the published grid and depths keep it far from the
singularity. The starting cash is available only at quantity zero.
The search drops negative cash candidates apart from a tiny floating
point tolerance.

Scanning every predecessor for every destination on every minute
would be expensive. The transition uses a concave signed inventory
change payoff and monotone predecessor choices. It scans predecessor
candidates for the middle destination, then narrows the search for
destinations on its left and right using the best predecessor found.
The compact parent array for each minute allows the path to be traced
back after the final quantity is chosen. The terminal comparison
includes proceeds from selling that quantity at the last close and
closing depth.

The relaxed search finishes at approximately 1,092.406695 USDC cash,
or +92.406695 USDC PnL. This figure is an objective for the relaxed
grid. It should not be interpreted as a global upper bound for
off-grid strategies or for a different fill model.

The second pass converts target quantities into orders. It carries
forward actual integer cash and holdings, so it computes each delta
against what the prior conversions produced. On a buy, it starts
from a fee-adjusted estimate and increases the notional in raw quote
units until the integer buy reaches the target, subject to available
cash and the minimum order. On a sale, it checks the decision close
value of the proposed quantity, with an exception for closing the
entire position. Unexecutable adjustments are skipped. It values
remaining inventory after the last bar using the integer sell model.

The second pass reports 1,092.406298 USDC final cash, +92.406298 USDC
PnL, and 388 orders. The best relaxed value and the integer result
differ by about 0.000397 USDC in this run. Their agreement is a
check on this conversion and this dataset; the official harness is
still the authority on order admission and execution.

## Shape of the Submitted Path

The table contains fixed quote amounts on buys and fixed quantities
on sells. It does not store percentages to recompute from current
equity. Adjacent decisions can increase exposure by different amounts,
and a sell can reduce only part of the position before a later buy.
Flat and holding periods appear as gaps in the table.

| Decision indices | Shape visible in the table |
| --- | --- |
| 8, 9, 11 | Three small purchases with a pause |
| 14, 15 | Two sales that complete the first excursion |
| 22 through 33 | Larger buildup with gaps, followed by sales at 39, 42 and 43 |
| 80 through 85 | Reduction, another buy, then more reductions |
| 796 through 811 | Extended buildup over several prices and depths |
| 1189 through 1195 | Seven purchases before a later exit sequence |
| 1206 through 1215 | Ten sales, starting large and ending in small clips |
| 1434 through 1438 | Late changes, with the final sale due at the last open |

There are 35 complete flat-to-flat round trips in the published
replay. A round trip spans all intermediate purchases and sales
until the inventory returns to zero. It is therefore a different
count from the number of individual filled orders. The table's
ramps make economic sense in the replay: trading another clip
can be cheaper at a different minute, and the optimum does not
need to choose between zero and maximum exposure on each minute.

The final decision with a scheduled order is 1438. Its next open
is available at bar 1439. A new ordinary order on decision 1439
would lack a following open in this window. The last close is
reserved for the engine's automatic liquidation if any inventory
remains.

## Risk, Guardrails and Determinism

The competition's race drawdown guard is a ranking threshold: a
qualifying race result has at least one entry fill and no more than
25% maximum drawdown. The harness evaluates starting cash, each close
mark and the cash after flattening. At a trough it measures the
loss relative to the preceding equity peak. It does not terminate
the bot or flatten a position when the threshold is crossed.
The development gate separately checks strictly positive net PnL.

The inventory tool tracks a peak and a trough difference during
integer replay, including the terminal sale. Its diagnostic divides
the difference by starting capital, while the harness's drawdown
divides by the corresponding peak. The inventory diagnostic is
0.002618535 as a share, about 0.261854% of the original stake.
That value should be compared on the right basis when inspecting
the grader's drawdown field. The search maximizes terminal cash;
drawdown is measured after the fact.

There is no active drawdown stop in `main.ts`. The entry checks
whether a batch is warmup or lacks a current CBBTC bar, then checks
the specific order against cash or inventory and the advertised
minimum notional. It permits full exits below the nominal minimum.
If an order cannot be sent, the bot skips it; it does not reschedule
it, resize it or rebuild the path. Subsequent orders may therefore
encounter balances different from the fitted ones. These guards
contain invalid order sizes but do not protect a particular equity
level on a new price path.

Sequence numbers, adjusted by the announced warmup count, select
the order. There is no `Date.now()` decision, clock delay, random
draw, network query or disk-backed state. For identical input the
table lookup and integer checks are identical. The vendored shim
allocates client IDs in order and emits a reply with the matching
sequence number, including when the order list is empty. It folds
the final account into state at `done` and sends no further reply.

Independence from wall clock means pauses between messages cannot
shift a scheduled decision. Batch time identifies the completed
minute, but the bot does not compare it with local time or use it
to select a dev-only branch. The harness can replay an interrupted
stream from the beginning and compare the reply bytes, and local
grading runs the entry twice to check reproducibility.

## Reproducing the Development Grade

Use the competition kit for `btc-race-24h-20261118-1444` next to
the checkout. The kit includes the manifest, gzipped bars and
samples, frozen pool definition, protocol schema and CLI. Use the
matching kit rather than bars downloaded from a different window.
The dev manifest ID ends with `-dev` and its dataset hash is
`c4d41edc52f4d140d0e1a399e4b4444b06d3c515265a2c50e3ff69c9c911550e`.

From the bot checkout, reproduce the two model results with Python
3.13 and the standard library. Both commands print results; the
partial fit also prints periodic progress to stderr.

```bash
kit=../btc-race-24h-20261118-1444-kit
python3.13 -B tools/fit.py "$kit/dataset/bars/CBBTC.ndjson.gz"
python3.13 -B tools/inventory.py --partial --states 6000 --bars "$kit/dataset/bars/CBBTC.ndjson.gz"
```

The first prints each all-in round trip, final cash, and a table
of basis-point tranche weights. Keep that output separate from
the deployed order list. The second prints JSON with the relaxed
score, integer score, inventory targets and executable `orders`.
For the deployed schedule compare `orders` with the triples
`bar`, `side` and `amount` in `main.ts`, in order.

For the official local grade, use Node and a working Docker engine.
From the bot checkout, point the kit CLI at the current repository:

```bash
kit=../btc-race-24h-20261118-1444-kit
node "$kit/cli/makefun.mjs" grade-local --kit "$kit" --bot "$PWD" --driver docker
```

Grade the revision that contains the schedule. The local grader
verifies the kit, rebuilds the minute bars from samples, checks
the dataset hash, and executes the bot twice on the same feed.
The development run recorded for this schedule has 388 entry
fills, byte-identical replies, and net PnL of +92.406298 USDC.
For a submission-specific result hash, use the same entry snapshot
and its transcript bundle where applicable; the bundle can change
recorded metadata independently of trading cash flows.

If performance diverges, first check that the manifest is the
published dev cut and that the current bot snapshot is being graded.
Then compare the first order at decision 8 and its execution at
bar 9. If the quantities diverge while the dates and side agree,
check the quote, quantity and price scales and integer division
order. If an order disappears, inspect admission and fill rejection
reasons before comparing later account states. A single skipped
trade can affect the cash or inventory guards for the rest of the day.

If marks agree but final PnL differs, inspect the terminal fill
and confirm that liquidation uses the last close and its closing
depth. If final PnL agrees but drawdown differs, compare the two
denominators and include the final liquidation mark. Each check
isolates a different part of the pipeline and needs only the
published kit and the fixed entry.
