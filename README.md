# freqtrade-delta-exchange

A standalone, **installable** [freqtrade](https://www.freqtrade.io/) adapter for
**Delta Exchange** (`delta.exchange`) futures trading — packaged as a proper
freqtrade plugin so it works with `uv pip install` / `pip install` **even if the
upstream pull request is never merged**.

## Why this exists

`ccxt.delta` exposes every required futures primitive (createOrder, setLeverage,
setMarginMode, stop-market / trigger orders, fetchPositions, fetchFundingRate,
and a testnet via `set_sandbox_mode`), but its `has` capability map
under-reports them — so freqtrade's generic-Exchange validation refuses it. This
adapter declares the real capabilities via `_ft_has` and maps the
order / margin / stop plumbing freqtrade expects.

## Status

- **Phase 1 (current): REST polling.** Fully functional for live + demo trading.
  No websocket streaming yet (freqtrade falls back to REST polling).
- **Phase 2 (TODO): native websockets** (`wss://socket.delta.exchange` candles /
  orders / positions / book), matching the official exchanges.

## International vs Delta India

The adapter supports both. Delta India is a **separate entity** with different
endpoints, markets, and currencies:

| | International Delta | **Delta India** |
|---|---|---|
| Config flag | (default) | `"india": true` |
| Demo host | `testnet-api.delta.exchange` | `cdn-ind.testnet.deltaex.org` |
| Live host | `api.delta.exchange` | `api.india.delta.exchange` |
| BTC perp symbol | `BTC/USDT:USDT` | `BTC/USD:USD` |
| Settle currency | USDT | USD (`stake_currency: "USD"`) |

When `"india": true`:
  - `"sandbox": true`  → **demo/testnet** `cdn-ind.testnet.deltaex.org` (paper, no real money)
  - `"sandbox": false` → **live** `api.india.delta.exchange`

> Note: `api.india.delta.exchange` is the documented production gateway for
> Delta India (see https://docs.delta.exchange — "Verify the Correct
> Environment"). The `cdn.india.deltaex.org` host serves the same backend but
> is not the documented live API.

### Capability gaps handled the official way

ccxt.delta under-reports several futures capabilities. Each is handled via
freqtrade's **documented extension points** — no global ccxt monkeypatch, no
false `has` flags:

- **Funding-rate history**: `fetchFundingRateHistory` raises "not supported
  yet". Override `_fetch_funding_rate_history` to return `[]` (freqtrade already
  gates it via `exchange_has("fetchFundingRateHistory")` → graceful skip, no
  back-adjustment).
- **Leverage tiers**: `fetchLeverageTiers` unsupported. Declare it in
  `_ft_has["exchange_has_overrides"]` and override `get_leverage_tiers()` to
  return a permissive 0–100x tier (falls back to the config pair whitelist in
  backtest, where live markets aren't loaded).
- **Margin mode**: `set_margin_mode` is `NotSupported` on Delta (defaults to
  cross); `set_margin_mode()` tolerates that silently.

### Dry-run smoke test

`user_data/strategies/dry_run_smoke/DryRunSmoke.py` is a minimal always-enter
strategy (1m, enter then exit next candle) for exercising the broker plumbing
without waiting on a real signal. Pair it with
`freqtrade-delta-exchange/examples/delta-india-demo-smoke.config.json`. For a
direct (no-strategy) order test, place/cancel via the adapter's ccxt client on
the demo host.

### 2FA (TOTP)

Delta API keys with 2FA enabled must sign every request with the current TOTP in
the request `password`. Supply the base32 secret via the gitignored secrets file
(`totp_secret`) and the adapter computes a fresh code at client init:

```json
{ "exchange": { "key": "...", "secret": "...", "totp_secret": "JGFRS7RYJWMJWDKV" } }
```

## Demo vs Live (International)

| Goal | Setting |
|------|---------|
| Paper / demo trading | `"sandbox": true` → `testnet-api.delta.exchange` |
| Live trading | `"sandbox": false` → `api.delta.exchange` |

> **Delta India note:** `sandbox: true` routes to the India demo/testnet host
> `cdn-ind.testnet.deltaex.org` (paper, no real money). `sandbox: false` is live
> `api.india.delta.exchange`.

## Install

```bash
# From PyPI (published)
pip install freqtrade-delta-exchange

# Or, from a local checkout (editable)
uv pip install -e ./freqtrade-delta-exchange
# or
pip install -e ./freqtrade-delta-exchange
```

## Use

```jsonc
// in your freqtrade config.json
{
  "exchange": {
    "name": "delta",             // resolves to the installed Delta adapter
    "sandbox": true,             // true = Delta testnet (demo/paper), false = live
    "key": "...",                // keep keys OUT of this file — see below
    "secret": "..."
  },
  "trading_mode": "futures",
  "margin_mode": "cross"
}
```

**Never commit API keys.** Put them in a separate gitignored file and layer it:

```bash
freqtrade trade -c config.json -c delta-secrets.json
```

`delta-secrets.json`:
```json
{ "exchange": { "key": "YOUR_KEY", "secret": "YOUR_SECRET" } }
```

## Demo vs Live

| Goal | Setting |
|------|---------|
| Paper / demo trading | `"sandbox": true` → `testnet-api.delta.exchange` |
| Live trading | `"sandbox": false` → `api.delta.exchange` |

## Contributing upstream

This adapter is structured to be submitted as a freqtrade PR (drop
`freqtrade_delta_exchange/delta.py` into `freqtrade/exchange/delta.py` and add
`Delta` to `freqtrade/exchange/__init__.py` + `SUPPORTED_EXCHANGES`).
