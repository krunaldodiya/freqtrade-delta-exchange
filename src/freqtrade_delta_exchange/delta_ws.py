"""Delta Exchange native websocket client for freqtrade.

ccxt.delta has no WS support. This module implements a custom async WS client
that subscribes to Delta's ``agg_trades`` channel and aggregates ticks into
OHLCV candles, exposing the ccxt-pro-compatible interface that freqtrade's
ExchangeWS wrapper expects: ``watch_ohlcv``, ``ohlcvs``, ``close``, ``has``.

Channel: agg_trades (public, no auth required)
Format:  {"f":[{"p":"63099.5","r":"t","s":"1.0","t":1784287726301541}],"sy":"BTCUSD","type":"agg_trades"}
         p=price, s=size, t=timestamp(microseconds), r=role(m/taker)

Candles are built by bucketing trades into timeframe intervals. The first
candle in a bucket sets open; every tick updates close/high/low; volume sums.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import websockets

logger = logging.getLogger(__name__)

# Delta WS endpoints (all public, no auth for market data)
WS_HOSTS = {
    "india_demo": "wss://socketv2.india.deltaex.org",
    "india_live": "wss://socket.india.deltaex.org",
    "intl_demo": "wss://testnet-socket.delta.exchange",
    "intl_live": "wss://socket.delta.exchange",
}

# Freqtrade timeframe -> seconds per candle
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "1w": 604800,
}

# Max candles to keep in cache per pair/timeframe
MAX_CANDLES = 1000

HEARTBEAT_INTERVAL = 15  # seconds


class DeltaWSClient:
    """Custom async WS client that mimics the ccxt-pro interface freqtrade expects.

    freqtrade's ExchangeWS calls:
        - await self.watch_ohlcv(pair, timeframe) -> list[[ts, o, h, l, c, v]]
        - self.ohlcvs (dict: pair -> {timeframe -> [[ts, o, h, l, c, v], ...]})
        - await self.close()
        - self.has (dict: must have watchOHLCV: True)

    This client subscribes to agg_trades and builds candles in-memory.
    """

    def __init__(self, config: dict[str, Any], india: bool = False, sandbox: bool = False) -> None:
        self._config = config
        self._india = india
        self._sandbox = sandbox

        # ccxt-compatible interface
        self.ohlcvs: dict[str, dict[str, list[list[float]]]] = {}
        self.has: dict[str, bool | None] = {
            "watchOHLCV": True,
            "watchTicker": False,
            "watchTrades": False,
            "watchOrderBook": False,
        }
        self.options: dict[str, Any] = {}
        self.markets: dict[str, Any] = {}
        self.id = "delta"

        # WS state
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = asyncio.Event()
        self._subscribed: set[tuple[str, str]] = set()  # (delta_symbol, resolution)
        self._pair_to_symbol: dict[str, str] = {}  # "BTC/USD:USD" -> "BTCUSD"
        self._symbol_to_pair: dict[str, str] = {}  # "BTCUSD" -> "BTC/USD:USD"
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self.session: Any = None  # freqtrade checks this attribute

    # ------------------------------------------------------------------ #
    # Public interface (ccxt-pro compatible)
    # ------------------------------------------------------------------ #

    async def watch_ohlcv(self, pair: str, timeframe: str, params: dict | None = None) -> list[list[float]]:
        """Subscribe to trades for a pair and return aggregated candles.

        Called by freqtrade's ExchangeWS in a continuous loop. Each call
        blocks until new trade data arrives, then returns the current candle
        buffer for this pair/timeframe.
        """
        self._loop = asyncio.get_running_loop()
        symbol = self._pair_to_symbol.get(pair, pair.split("/")[0] + pair.split("/")[1].split(":")[0])
        key = (pair, timeframe)

        # Ensure connected
        if self._ws is None:
            await self._connect()

        # Subscribe to trades if not already (one subscribe per symbol)
        if not any(k[0] == pair for k in self._subscribed):
            await self._subscribe(symbol)

        # Register this pair/timeframe for candle building
        self._subscribed.add(key)

        # Wait for data to arrive (or return existing buffer)
        await asyncio.sleep(0.05)  # small yield for trades to flow in
        candles = self.ohlcvs.get(pair, {}).get(timeframe, [])
        return candles

    async def close(self) -> None:
        """Close the WS connection and stop background tasks."""
        self._closed = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._listen_task:
            self._listen_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._connected.clear()

    def set_markets_from_exchange(self, other: Any) -> None:
        """Copy markets from the REST ccxt instance (freqtrade calls this)."""
        if hasattr(other, "markets"):
            self.markets = other.markets
            # Build pair <-> symbol mapping from markets
            for ft_pair, market in self.markets.items():
                delta_sym = market.get("id", "")
                if delta_sym:
                    self._pair_to_symbol[ft_pair] = delta_sym
                    self._symbol_to_pair[delta_sym] = ft_pair

    # ------------------------------------------------------------------ #
    # Internal: connection, subscription, trade processing
    # ------------------------------------------------------------------ #

    def _ws_url(self) -> str:
        if self._india:
            return WS_HOSTS["india_demo"] if self._sandbox else WS_HOSTS["india_live"]
        return WS_HOSTS["intl_demo"] if self._sandbox else WS_HOSTS["intl_live"]

    async def _connect(self) -> None:
        """Connect to Delta WS and start listening."""
        url = self._ws_url()
        logger.info(f"Delta WS connecting to {url}")
        self._ws = await websockets.connect(url, ping_interval=30, ping_timeout=60)
        # Enable heartbeat
        await self._ws.send('{"type":"enable_heartbeat"}')
        # Start background listener
        self._listen_task = asyncio.create_task(self._listen())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._connected.set()
        logger.info("Delta WS connected")

    async def _subscribe(self, symbol: str) -> None:
        """Subscribe to agg_trades for a Delta product symbol."""
        import json
        msg = json.dumps({"type": "subscribe", "payload": {"channels": [{"name": "agg_trades", "symbols": [symbol]}]}})
        await self._ws.send(msg)
        logger.info(f"Delta WS subscribed to agg_trades: {symbol}")

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat to keep connection alive."""
        while not self._closed and self._ws and not self._ws.closed:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws and not self._ws.closed:
                    await self._ws.send('{"type":"heartbeat"}')
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Heartbeat send failed", exc_info=True)
                break

    async def _listen(self) -> None:
        """Background task: receive and process WS messages."""
        import json

        while not self._closed and self._ws:
            try:
                raw = await self._ws.recv()
                data = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "agg_trades":
                    self._process_trades(data)
                elif msg_type == "subscriptions":
                    logger.debug(f"WS sub confirmed: {data.get('channels', [])}")
                elif msg_type == "heartbeat":
                    pass  # server heartbeat, ignore
                elif msg_type == "error":
                    logger.warning(f"WS error: {data.get('message', raw)}")
                else:
                    logger.debug(f"WS msg type={msg_type}: {raw[:200]}")

            except websockets.ConnectionClosed:
                logger.info("Delta WS connection closed")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Delta WS listen error: {e}")
                break

    def _process_trades(self, data: dict) -> None:
        """Aggregate trade ticks into OHLCV candles for all subscribed TFs."""
        symbol = data.get("sy", "")
        pair = self._symbol_to_pair.get(symbol)
        if not pair:
            # Try to map: BTCUSD -> BTC/USD:USD (fallback for unmapped symbols)
            return

        trades = data.get("f", [])
        for trade in trades:
            price = float(trade["p"])
            size = float(trade["s"])
            ts_us = int(trade["t"])  # microseconds
            ts_sec = ts_us // 1_000_000

            for (p, tf) in self._subscribed:
                if p != pair:
                    continue
                bucket = ts_sec - (ts_sec % TF_SECONDS.get(tf, 60))
                bucket_ms = bucket * 1000
                self._update_candle(pair, tf, bucket_ms, price, size)

    def _update_candle(self, pair: str, tf: str, bucket_ms: int, price: float, size: float) -> None:
        """Update or create a candle for the given pair/timeframe/bucket."""
        if pair not in self.ohlcvs:
            self.ohlcvs[pair] = {}
        if tf not in self.ohlcvs[pair]:
            self.ohlcvs[pair][tf] = []

        candles = self.ohlcvs[pair][tf]

        if candles and candles[-1][0] == bucket_ms:
            # Update existing candle
            c = candles[-1]
            c[2] = max(c[2], price)  # high
            c[3] = min(c[3], price)  # low
            c[4] = price  # close (latest trade)
            c[5] += size  # volume
        else:
            # New candle
            candles.append([bucket_ms, price, price, price, price, size])
            # Trim old candles
            if len(candles) > MAX_CANDLES:
                self.ohlcvs[pair][tf] = candles[-MAX_CANDLES:]