"""freqtrade-delta-exchange package.

Importing this package registers the Delta Exchange adapter into freqtrade's
exchange registry so freqtrade's ExchangeResolver can find it — without patching
the installed freqtrade package. This is the supported mechanism given that
freqtrade does not (yet) load custom exchanges via entry points or user_data/.

Funding-rate history: ccxt.delta does not implement fetchFundingRateHistory, and
freqtrade already gates it natively via `exchange_has("fetchFundingRateHistory")`
(see exchange.check_candle_type_support / _fetch_funding_rate_history). We simply
do NOT declare it in _ft_has_futures, so freqtrade gracefully skips funding-rate
history — no global ccxt monkeypatch needed.
"""
from freqtrade_delta_exchange.delta import Delta

# Register into freqtrade.exchange so ExchangeResolver's
# `getattr(freqtrade.exchange, "Delta")` lookup succeeds.
try:
    import freqtrade.exchange as _ft_exchange
    from freqtrade.exchange.common import SUPPORTED_EXCHANGES

    setattr(_ft_exchange, "Delta", Delta)

    # Also declare support so validation / list-exchanges recognise it.
    if "delta" not in SUPPORTED_EXCHANGES:
        SUPPORTED_EXCHANGES.append("delta")
except Exception:  # pragma: no cover - registry best-effort
    pass

__all__ = ["Delta"]
