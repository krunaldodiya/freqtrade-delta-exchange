# freqtrade-delta-exchange

Trade on **Delta Exchange** with [freqtrade](https://www.freqtrade.io/).
This package plugs Delta into freqtrade — install it and `"name": "delta"`
works in your freqtrade config, just like any built-in exchange.

Works with both **Delta International** and **Delta India**, live and demo
(paper) accounts.

## Install

```bash
pip install freqtrade-delta-exchange
```

That's it — no freqtrade fork or patch needed. To confirm it registered:

```bash
freqtrade list-exchanges | grep -i delta
```

## Quick start

You need two files: a **config** and a **secrets** file (for your API keys).

**1. `config.json`** — trading settings:

```json
{
  "dry_run": true,
  "trading_mode": "futures",
  "margin_mode": "cross",
  "stake_currency": "USD",
  "stake_amount": 100,
  "max_open_trades": 1,
  "timeframe": "1h",
  "exchange": {
    "name": "delta",
    "india": true,
    "sandbox": true,
    "key": "",
    "secret": "",
    "pair_whitelist": ["BTC/USD:USD"],
    "pair_blacklist": []
  },
  "pairlists": [{ "method": "StaticPairList" }]
}
```

**2. `delta-secrets.json`** — your API keys (never commit this file):

```json
{
  "exchange": {
    "key": "YOUR_API_KEY",
    "secret": "YOUR_API_SECRET"
  }
}
```

**3. Run:**

```bash
freqtrade trade -c config.json -c delta-secrets.json
```

## The 3 settings that matter

| Setting | Values | What it does |
|---|---|---|
| `exchange.name` | `"delta"` | Use this adapter. |
| `exchange.india` | `true` / `false` | `true` = Delta India (`BTC/USD:USD`, settles in **USD**). `false` = Delta International (`BTC/USDT:USDT`, settles in **USDT**). |
| `exchange.sandbox` | `true` / `false` | `true` = demo account (paper, fake money). `false` = live account (**real money**). |

> **Start with `"sandbox": true` + `"dry_run": true`.** When that works,
> try `"sandbox": true` + `"dry_run": false` to see real orders on your demo
> dashboard. Only flip `sandbox` to `false` when you're ready to go live.

### Matching pairs and stake currency

| | Delta India | Delta International |
|---|---|---|
| `pair_whitelist` | `"BTC/USD:USD"` | `"BTC/USDT:USDT"` |
| `stake_currency` | `"USD"` | `"USDT"` |

Using the wrong pair/currency combo is the most common setup mistake —
the pair must match the account type.

## API keys & 2FA

Create keys in your Delta account (demo keys from the demo site, live keys
from the live site — they are **not** interchangeable).

If your key has 2FA enabled, add the base32 TOTP secret to the secrets file:

```json
{
  "exchange": {
    "key": "YOUR_API_KEY",
    "secret": "YOUR_API_SECRET",
    "totp_secret": "JGFRS7RYJWMJWDKV"
  }
}
```

## Examples

Ready-made configs live in [`examples/`](examples/):

- `delta-india-demo-smoke.config.json` — India demo account, 1-minute smoke test
- `delta-india-demo.config.json` — India demo account
- `delta-india-live.config.json` — India live account

## Limitations

- Funding-rate history is not available on Delta (no API endpoint), so
  backtests don't back-adjust funding fees.
- Leverage tiers are not published by Delta; the adapter assumes a flat
  0.5% maintenance margin for liquidation estimates.

## Links

- Delta API docs: https://docs.delta.exchange
- freqtrade docs: https://www.freqtrade.io
