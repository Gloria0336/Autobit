from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import numpy as np
import pandas as pd

from config import SIGNAL_LIMIT, TREND_LIMIT
from historical_data import HistoricalDataError, HistoricalFxService, interval_to_timedelta, resample_ohlcv
from live_market_data import MarketDataFetcher
from web_models import SimulationConfig


class PlaybackCompleted(Exception):
    """Raised when a historical playback source has no more candles."""


@dataclass
class MarketSnapshot:
    price: float
    fx_rate: float
    fx_date: str
    trend_df: pd.DataFrame
    signal_df: pd.DataFrame
    market_timestamp: datetime | None = None
    playback_index: int | None = None
    playback_total: int | None = None


class MarketDataSource(Protocol):
    def get_next_snapshot(self) -> MarketSnapshot:
        ...


class LiveMarketDataSource:
    def __init__(self, fetcher: MarketDataFetcher, config: SimulationConfig) -> None:
        self.fetcher = fetcher
        self.config = config

    def get_next_snapshot(self) -> MarketSnapshot:
        fx_rate, fx_date = self.fetcher.get_display_fx_rate()
        trend_df = self.fetcher.get_klines(self.config.symbol, self.config.trend_interval, TREND_LIMIT)
        signal_df = self.fetcher.get_klines(self.config.symbol, self.config.signal_interval, SIGNAL_LIMIT)
        price = self.fetcher.get_current_price(self.config.symbol)
        return MarketSnapshot(
            price=price,
            fx_rate=fx_rate,
            fx_date=fx_date,
            trend_df=trend_df,
            signal_df=signal_df,
        )


class HistoricalPlaybackSource:
    def __init__(
        self,
        historical_df: pd.DataFrame,
        *,
        base_interval: str,
        config: SimulationConfig,
        fx_service: HistoricalFxService,
    ) -> None:
        self.config = config
        self.fx_service = fx_service
        self.base_interval = base_interval
        self.base_delta = interval_to_timedelta(base_interval)
        self.base_df = historical_df.sort_values("timestamp").reset_index(drop=True).copy()
        if "close_time" not in self.base_df.columns:
            self.base_df["close_time"] = self.base_df["timestamp"] + self.base_delta

        self.trend_df = resample_ohlcv(self.base_df, config.trend_interval)
        self.signal_df = resample_ohlcv(self.base_df, config.signal_interval)
        self._playable_indices = self._build_playable_indices()
        self._cursor = 0

    @property
    def playback_total(self) -> int:
        return len(self._playable_indices)

    def get_market_dates(self) -> list[date]:
        if not self._playable_indices:
            return []
        close_times = self.base_df.loc[self._playable_indices, "close_time"]
        return [timestamp.date() for timestamp in np.array(close_times.dt.to_pydatetime())]

    def get_next_snapshot(self) -> MarketSnapshot:
        if self._cursor >= len(self._playable_indices):
            raise PlaybackCompleted()

        base_index = self._playable_indices[self._cursor]
        playback_index = self._cursor + 1
        self._cursor += 1

        row = self.base_df.iloc[base_index]
        market_timestamp = self._to_datetime(row["close_time"])
        trend_df = self._select_closed_window(self.trend_df, row["close_time"], TREND_LIMIT)
        signal_df = self._select_closed_window(self.signal_df, row["close_time"], SIGNAL_LIMIT)
        fx_rate, fx_date = self.fx_service.get_rate_for_date(market_timestamp.date())

        return MarketSnapshot(
            price=float(row["close"]),
            fx_rate=fx_rate,
            fx_date=fx_date,
            trend_df=trend_df,
            signal_df=signal_df,
            market_timestamp=market_timestamp,
            playback_index=playback_index,
            playback_total=len(self._playable_indices),
        )

    def _build_playable_indices(self) -> list[int]:
        trend_close = self.trend_df["close_time"].to_numpy(dtype="datetime64[ns]")
        signal_close = self.signal_df["close_time"].to_numpy(dtype="datetime64[ns]")
        playable_indices: list[int] = []
        for index, close_time in enumerate(self.base_df["close_time"].to_numpy(dtype="datetime64[ns]")):
            trend_count = int(np.searchsorted(trend_close, close_time, side="right"))
            signal_count = int(np.searchsorted(signal_close, close_time, side="right"))
            if trend_count >= TREND_LIMIT and signal_count >= SIGNAL_LIMIT:
                playable_indices.append(index)
        if not playable_indices:
            raise HistoricalDataError("Historical data does not contain a playable window after indicator warmup")
        return playable_indices

    def _select_closed_window(self, frame: pd.DataFrame, close_time: pd.Timestamp, limit: int) -> pd.DataFrame:
        closed = frame[frame["close_time"] <= close_time].tail(limit).copy()
        return closed[["timestamp", "open", "high", "low", "close", "volume"]].rename(
            columns={"timestamp": "open_time"}
        )

    def _to_datetime(self, value: pd.Timestamp) -> datetime:
        timestamp = value.to_pydatetime()
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=self.base_df["timestamp"].dt.tz)
        return timestamp
