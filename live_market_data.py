import logging
import math
from datetime import datetime, timezone

import pandas as pd
import requests

from config import (
    BINANCE_BASE_URL,
    DISPLAY_CURRENCY,
    FRANKFURTER_BASE_URL,
    FX_RATE_BASE,
    FX_RATE_QUOTE,
    KLINES_ENDPOINT,
    REQUEST_TIMEOUT,
    SYMBOL,
    TICKER_ENDPOINT,
    TRADING_CURRENCY,
)

log = logging.getLogger("autobit")

_INTERVAL_TO_SECONDS = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}


class DataFetchError(Exception):
    """Raised when market data or FX data cannot be fetched."""


class DataIntegrityError(DataFetchError):
    """Raised when live market data looks stale, malformed, or synthetic."""


class MarketDataFetcher:
    def __init__(self, base_url: str = BINANCE_BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._fx_rate: float | None = None
        self._fx_date: str | None = None

    def get_current_price(self, symbol: str = SYMBOL) -> float:
        url = f"{self.base_url}{TICKER_ENDPOINT}"
        try:
            resp = self.session.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            price = float(resp.json()["price"])
            if not math.isfinite(price) or price <= 0:
                raise DataIntegrityError(f"Rejected non-live price payload for {symbol}: {price!r}")
            return price
        except DataFetchError:
            raise
        except Exception as e:
            raise DataFetchError(f"Failed to fetch current price: {e}") from e

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        url = f"{self.base_url}{KLINES_ENDPOINT}"
        params = {"symbol": symbol, "interval": interval, "limit": limit + 1}
        try:
            resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            df = self._parse_klines(resp.json())
            return self._sanitize_klines(df, interval, limit)
        except DataFetchError:
            raise
        except Exception as e:
            raise DataFetchError(f"Failed to fetch klines for {interval}: {e}") from e

    def get_display_fx_rate(self) -> tuple[float, str]:
        """
        Use USD/TWD to approximate USDT/TWD for display-only conversion.
        Trading logic remains on BTCUSDT.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        if self._fx_rate is not None and self._fx_date == today:
            return self._fx_rate, self._fx_date

        url = f"{FRANKFURTER_BASE_URL}/v2/rate/{FX_RATE_BASE}/{FX_RATE_QUOTE}"
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            self._fx_rate = float(payload["rate"])
            self._fx_date = str(payload["date"])
            return self._fx_rate, self._fx_date
        except Exception as e:
            if self._fx_rate is not None and self._fx_date is not None:
                log.warning(
                    "FX refresh failed; reusing display-only FX cache from %s for %s/%s at %.4f (%s -> %s): %s",
                    self._fx_date,
                    FX_RATE_BASE,
                    FX_RATE_QUOTE,
                    self._fx_rate,
                    TRADING_CURRENCY,
                    DISPLAY_CURRENCY,
                    e,
                )
                return self._fx_rate, self._fx_date
            raise DataFetchError(
                f"Failed to fetch {FX_RATE_BASE}/{FX_RATE_QUOTE} FX rate for display conversion: {e}"
            ) from e

    def _parse_klines(self, raw: list) -> pd.DataFrame:
        if not raw:
            raise DataFetchError("Exchange returned no kline data")
        df = pd.DataFrame(
            raw,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df[
            ["open_time", "open", "high", "low", "close", "volume", "close_time"]
        ].reset_index(drop=True)

    def _sanitize_klines(
        self,
        df: pd.DataFrame,
        interval: str,
        limit: int,
        now: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        interval_delta = self._interval_to_timedelta(interval)
        numeric_cols = ["open", "high", "low", "close", "volume"]

        if len(df) < limit:
            raise DataIntegrityError(f"Expected at least {limit} candles, got {len(df)}")
        if not df["open_time"].is_monotonic_increasing:
            raise DataIntegrityError(f"Rejected out-of-order {interval} candles")
        if df["open_time"].duplicated().any():
            raise DataIntegrityError(f"Rejected duplicate {interval} candle timestamps")
        if df[numeric_cols].isnull().any().any():
            raise DataIntegrityError(f"Rejected malformed {interval} candles with missing numeric fields")
        if (df[["open", "high", "low", "close"]] <= 0).any().any():
            raise DataIntegrityError(f"Rejected non-positive {interval} OHLC values")
        if (df["volume"] < 0).any():
            raise DataIntegrityError(f"Rejected negative {interval} candle volume")

        invalid_price_shape = (
            (df["high"] < df["low"])
            | (df["open"] > df["high"])
            | (df["open"] < df["low"])
            | (df["close"] > df["high"])
            | (df["close"] < df["low"])
        )
        if invalid_price_shape.any():
            raise DataIntegrityError(f"Rejected impossible {interval} OHLC candle shape")

        now_utc = now or pd.Timestamp.now(tz="UTC")
        closed_df = df[df["close_time"] <= now_utc].copy()
        if len(closed_df) < limit:
            raise DataIntegrityError(
                f"Need {limit} closed {interval} candles, only received {len(closed_df)}"
            )

        sanitized = closed_df.tail(limit).reset_index(drop=True)
        cadence = sanitized["open_time"].diff().dropna()
        if not cadence.empty and not cadence.eq(interval_delta).all():
            raise DataIntegrityError(f"Rejected gapped or irregular {interval} candle series")

        return sanitized[["open_time", "open", "high", "low", "close", "volume"]]

    def _interval_to_timedelta(self, interval: str) -> pd.Timedelta:
        seconds = _INTERVAL_TO_SECONDS.get(interval)
        if seconds is None:
            raise DataIntegrityError(f"Unsupported interval for live validation: {interval}")
        return pd.Timedelta(seconds=seconds)
