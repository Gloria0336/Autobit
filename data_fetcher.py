import logging
import requests
import pandas as pd
from config import (
    BINANCE_BASE_URL, KLINES_ENDPOINT, TICKER_ENDPOINT,
    SYMBOL, REQUEST_TIMEOUT
)

log = logging.getLogger("autobit")


class DataFetchError(Exception):
    """幣安 API 取得資料失敗時拋出。"""


class BinanceFetcher:
    def __init__(self, base_url: str = BINANCE_BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def get_current_price(self, symbol: str = SYMBOL) -> float:
        """取得最新即時成交價（單位：TWD）。"""
        url = f"{self.base_url}{TICKER_ENDPOINT}"
        try:
            resp = self.session.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return float(resp.json()["price"])
        except Exception as e:
            raise DataFetchError(f"無法取得即時價格：{e}") from e

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """
        取得 K 線（OHLCV）資料，回傳 DataFrame。
        欄位：open_time, open, high, low, close, volume
        """
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

    def _parse_klines(self, raw: list) -> pd.DataFrame:
        if not raw:
            raise DataFetchError("幣安回傳空的 K 線資料。")
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df[["open_time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


from live_market_data import DataFetchError as DataFetchError
from live_market_data import MarketDataFetcher as BinanceFetcher
