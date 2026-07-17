# pragma pylint: disable=missing-docstring, invalid-name, too-many-ancestors
# flake8: noqa: F401
"""
Delta Exchange broker adapter for freqtrade (Phase 1: REST).

Proper freqtrade Exchange subclass, packaged as an installable freqtrade plugin
so it works with `uv pip install -e .` / `pip install .` even if the upstream PR
is never merged.

Why this adapter exists:
  ccxt.delta exposes every required futures primitive (createOrder, setLeverage,
  setMarginMode, stop-market/trigger orders, fetchPositions, fetchFundingRate,
  testnet via set_sandbox_mode) but its `has` capability map under-reports them,
  so freqtrade's generic-Exchange validation refuses it. This subclass fixes the
  capability flags via `_ft_has` and maps the order/margin/stop plumbing
  freqtrade expects.

Phase 1 = REST polling (freqtrade's documented fallback).
Phase 2 = native asyncio websocket client (delta_ws.DeltaWSClient) that
  subscribes to Delta's agg_trades channel and builds OHLCV candles in-memory,
  feeding freqtrade's ExchangeWS pipeline — matching official exchanges.

Hosts / environments:
  - International Delta (default): api.delta.exchange, demo = testnet-api.delta.exchange.
  - Delta India (exchange.india = true): cdn.india.deltaex.org (live),
    cdn-ind.testnet.deltaex.org (demo). BTC perp symbol = BTC/USD:USD, USD-settled.

2FA: Delta API keys with 2FA enabled must sign every request with the current TOTP
  in the request `password`. The adapter reads `exchange.totp_secret` (supplied via a
  gitignored secrets file) and injects a fresh TOTP at client init.

NOTE: API keys/secret/totp_secret go in a SEPARATE gitignored delta-secrets.json and
are merged via `freqtrade ... --config config.json --config delta-secrets.json`.
Never commit credentials.
"""
from typing import Any

import ccxt
import logging
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas

logger = logging.getLogger(__name__)


class Delta(Exchange):
    """Delta Exchange (delta.exchange) futures broker."""

    _is_default = False  # not a built-in; loaded via the freqtrade plugin entry point

    _ft_has: FtHas = {
        "ohlcv_has_history": True,
        "ws_enabled": True,  # Phase2: native WS via DeltaWSClient
        "stoploss_on_exchange": True,
        "exchange_has_overrides": {
            # ccxt.delta under-reports these; we implement them in the class
            # (get_leverage_tiers) or make them no-ops (funding history).
            "fetchLeverageTiers": True,
            # freqtrade calls _api.fetch_funding_history for open-trade fee calc;
            # we bind a no-op on our client instance in _init_ccxt.
            "fetchFundingHistory": True,
            # WS: our DeltaWSClient implements watch_ohlcv
            "watchOHLCV": True,
        },
    }
    _ft_has_futures: FtHas = {
        "ohlcv_has_history": True,
        "funding_fee_candle_limit": 200,

        "stoploss_on_exchange": True,
        "stoploss_order_types": {"limit": "limit", "market": "market"},
        "stoploss_blocks_assets": False,
        "stop_price_prop": "stopPrice",
        "exchange_has_overrides": {},
        "has_delisting": True,
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.FUTURES, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    @property
    def name(self) -> str:
        return "Delta"

    # ------------------------------------------------------------------ #
    # ccxt configuration: sandbox (testnet/demo) + USDT-swap default
    # ------------------------------------------------------------------ #
    @property
    def _ccxt_config(self) -> dict:
        # USDT-settled linear swaps for futures mode.
        config: dict[str, Any] = {}
        if self.trading_mode == TradingMode.FUTURES:
            config.update({"options": {"defaultType": "swap"}})
        return config

    def _init_ccxt(self, exchange_config: dict[str, Any], sync: bool, ccxt_kwargs: dict[str, Any]):
        """Build the ccxt client, route to the right Delta host, inject 2FA TOTP.

        Phase 2: freqtrade calls _init_ccxt twice:
          - sync=True  -> REST sync client (self._api)
          - sync=False  -> REST async client (self._api_async) + WS client (self._ws_async)
        Both sync/async return a ccxt.delta instance with India host routing.
        The WS client (DeltaWSClient) is created separately by freqtrade at line 289
        via self._ws_async = self._init_ccxt(...) — but we can't distinguish that
        call from the _api_async call. Instead, we override the property that
        freqtrade checks for WS support so ExchangeWS gets our DeltaWSClient.

        Host routing:
          - india + sandbox=true  -> cdn-ind.testnet.deltaex.org  (DEMO / paper)
          - india + sandbox=false -> cdn.india.deltaex.org        (LIVE)
          - international + sandbox=true -> set_sandbox_mode(True) (testnet-api.delta.exchange)
          - international + sandbox=false -> api.delta.exchange    (live)
        urls["api"] must stay a dict of sub-apis (never a bare string).
        TOTP: if exchange.totp_secret is present (from a gitignored secrets file),
        compute the current 2FA code and set api.password so every signed request
        is authenticated. Rotates every 30s; computed at init per run.
        """
        # Both sync and async paths get a ccxt.delta instance with India routing.
        api = super()._init_ccxt(exchange_config, sync, ccxt_kwargs)
        ex_cfg = self._config.get("exchange", {})
        if ex_cfg.get("india"):
            # Delta India has a separate demo/testnet host (not set_sandbox_mode).
            host = (
                "https://cdn-ind.testnet.deltaex.org"
                if ex_cfg.get("sandbox")
                else "https://cdn.india.deltaex.org"
            )
            api.urls["api"] = {"public": host, "private": host}
        elif self._config.get("dry_run") or ex_cfg.get("sandbox"):
            if hasattr(api, "set_sandbox_mode"):
                api.set_sandbox_mode(True)
        # 2FA TOTP (Delta requires it in the request password when enabled on the key)
        totp_secret = ex_cfg.get("totp_secret")
        if totp_secret:
            api.password = self._totp(totp_secret)
        # freqtrade requires fetch_funding_history for open-trade fee calc.
        # ccxt.delta doesn't implement it; bind a scoped no-op on OUR client
        # instance (not a global ccxt monkeypatch) so funding fees are skipped.
        api.fetch_funding_history = lambda symbol=None, since=None, params=None: []
        return api

    @staticmethod
    def _totp(b32: str) -> str:
        """RFC6238 TOTP (6 digits, 30s step) from a base32 2FA secret."""
        import base64
        import hashlib
        import hmac as _hmac
        import struct
        import time

        key = base64.b32decode(b32.upper() + "=" * ((8 - len(b32) % 8) % 8))
        counter = int(time.time() // 30)
        msg = struct.pack(">Q", counter)
        digest = _hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** 6
        return f"{code:06d}"

    # ------------------------------------------------------------------ #
    # Phase 2: swap freqtrade's WS async client with our DeltaWSClient
    # ------------------------------------------------------------------ #
    def ft_additional_exchange_init(self) -> None:
        """Called by Exchange.__init__ after markets load. Replace the WS
        async client (which is a plain ccxt.delta with no watch_ohlcv) with
        our custom DeltaWSClient that implements native WS streaming.
        """
        if not self._ft_has.get("ws_enabled"):
            return
        if not self._exchange_ws:
            return
        from .delta_ws import DeltaWSClient
        ex_cfg = self._config.get("exchange", {})
        ws_client = DeltaWSClient(
            config=self._config,
            india=ex_cfg.get("india", False),
            sandbox=ex_cfg.get("sandbox", False),
        )
        # Copy markets from the REST client for pair<->symbol mapping
        ws_client.set_markets_from_exchange(self._api)
        # Replace the ccxt object inside ExchangeWS
        self._exchange_ws._ccxt_object = ws_client
        logger.info("Delta WS: DeltaWSClient installed for native streaming")

    # ------------------------------------------------------------------ #
    # Margin / leverage  (signatures match freqtrade.exchange.Exchange)
    # ------------------------------------------------------------------ #
    def set_margin_mode(
        self,
        pair: str,
        margin_mode: MarginMode,
        accept_fail: bool = False,
        params: dict | None = None,
    ) -> None:
        """Set cross/isolated margin. Delta's ccxt set_margin_mode is reported as
        NotSupported (it defaults to cross); tolerate that gracefully so dry-run
        / live don't abort. Honour accept_fail for genuine failures.
        """
        try:
            mode = "cross" if str(margin_mode).lower().startswith("cross") else "isolated"
            self._api.set_margin_mode(mode, pair, params or {})
        except ccxt.NotSupported:
            # Delta uses cross margin by default; nothing to do.
            pass
        except Exception:
            if not accept_fail:
                raise

    # ------------------------------------------------------------------ #
    # Funding (freqtrade futures reads a ccxt funding-rate dict)
    # ------------------------------------------------------------------ #
    def fetch_funding_rate(self, pair: str, **kwargs) -> Any:
        """Return the ccxt funding-rate dict for the pair."""
        rate = self._api.fetch_funding_rate(pair)
        return {
            "info": rate.get("info", {}),
            "symbol": pair,
            "markPrice": rate.get("markPrice"),
            "indexPrice": rate.get("indexPrice"),
            "interestRate": rate.get("interestRate", 0.0),
            "estimatedSettlePrice": rate.get("estimatedSettlePrice"),
            "timestamp": int(rate.get("timestamp", 0) or 0),
            "datetime": rate.get("datetime"),
            "fundingRate": float(rate.get("fundingRate") or rate.get("rate") or 0.0),
            "fundingTimestamp": int(rate.get("nextFundingTime", 0) or 0),
            "fundingDatetime": rate.get("nextFundingTime") and str(rate.get("nextFundingTime")),
            "nextFundingTimestamp": int(rate.get("nextFundingTime", 0) or 0),
            "nextFundingDatetime": rate.get("nextFundingTime") and str(rate.get("nextFundingTime")),
        }

    # ------------------------------------------------------------------ #
    # Stop orders (your strategy relies on stops)
    # ------------------------------------------------------------------ #
    def create_stoploss(
        self,
        pair: str,
        amount: float,
        stop_price: float,
        order_types: dict,
        side: str,
        leverage: float = 1.0,
    ) -> dict:
        """Place a stop-market order on Delta to close the open position.

        Maps freqtrade's entry 'side' to the opposite close side.
        """
        close_side = "sell" if side == "buy" else "buy"
        return self._api.create_stop_market_order(
            pair,
            close_side,
            amount,
            stop_price,
            {"reduceOnly": True, "leverage": leverage},
        )

    # ------------------------------------------------------------------ #
    # Funding rate history (freqtrade calls self._fetch_funding_rate_history)
    # ------------------------------------------------------------------ #
    async def _fetch_funding_rate_history(
        self, pair: str, timeframe: str, limit: int, since_ms: int | None = None
    ) -> list:
        """Delta has no funding-rate history endpoint.

        ccxt.delta's fetchFundingRateHistory raises "not supported yet", which
        freqtrade wraps as OperationalException and aborts OHLCV download. We
        override freqtrade's documented subclass hook to return an empty list
        instead — funding fees simply aren't back-adjusted. This is scoped to
        the Delta class (no global ccxt monkeypatch) and requires no false
        `has` capability flag.
        """
        return []

    # ------------------------------------------------------------------ #
    # Leverage tiers (ccxt.delta has no fetchLeverageTiers)
    # ------------------------------------------------------------------ #
    def get_leverage_tiers(self) -> dict[str, list[dict]]:
        """Delta exposes no leverage-tier endpoint; freqtrade needs tiers for
        futures backtesting/stake sizing. Return a single permissive tier per
        market. In backtest, live markets aren't loaded, so fall back to the
        configured pair whitelist (and any loaded markets) — scoped to this
        class, no ccxt monkeypatch.
        """
        tier = {
            "minNotional": 0.0,
            "maxNotional": 1e12,
            "maintenanceMarginRate": 0.005,
            "maxLeverage": 100.0,
            "minLeverage": 1.0,
        }
        symbols = list(self.markets.keys())
        if not symbols:
            symbols = list(self._config.get("exchange", {}).get("pair_whitelist", []))
        return {symbol: [dict(tier)] for symbol in symbols}
