import logging
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


class DataFetchError(Exception):
    """Raised when market data or FX data cannot be fetched."""


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
            return float(resp.json()["price"])
        except Exception as e:
            raise DataFetchError(f"無法取得即時價格：{e}") from e

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        url = f"{self.base_url}{KLINES_ENDPOINT}"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        try:
            resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return self._parse_klines(resp.json())
        except DataFetchError:
            raise
        except Exception as e:
            raise DataFetchError(f"無法取得 K 線資料 ({interval})：{e}") from e

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
                    "匯率更新失敗，沿用 %s 的 %s/%s 匯率 %.4f（%s -> %s 顯示近似）：%s",
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
                f"無法取得 {FX_RATE_BASE}/{FX_RATE_QUOTE} 匯率（用於 {TRADING_CURRENCY} -> {DISPLAY_CURRENCY} 顯示換算）：{e}"
            ) from e

    def _parse_klines(self, raw: list) -> pd.DataFrame:
        if not raw:
            raise DataFetchError("幣安回傳空的 K 線資料。")
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
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df[["open_time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


from live_market_data import DataFetchError as DataFetchError
from live_market_data import DataIntegrityError as DataIntegrityError
from live_market_data import MarketDataFetcher as MarketDataFetcher
