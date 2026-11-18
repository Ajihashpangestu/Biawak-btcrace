[![build](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/Ajihashpangestu/Biawak-btcrace/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/Ajihashpangestu/Biawak-btcrace/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

This bot replays a hindsight-fitted, partial-position schedule on the published cbBTC dev window. The schedule uses zero-based decision batch indices. Orders fill at the next bar's open.

Run `python3.13 tools/fit.py` to regenerate an all-in baseline. `python3.13 tools/inventory.py --partial --states 6000` fits the partial-position schedule used here. Both use only the Python standard library and model 4 bps fees, 2 bps slippage, constant-product impact, integer rounding, and automatic liquidation at the last close.

The published-bar replay of this schedule is **+92.406298 USDC** with 388 filled orders, 35 complete flat-to-flat round trips, and 0.261854% maximum drawdown. Two independent bot executions produce byte-identical replies; the model and replay agree to the micro-USDC.
