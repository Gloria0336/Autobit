from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from pandas.api.types import is_numeric_dtype

from config import (
    FRANKFURTER_BASE_URL,
    FX_RATE_BASE,
    FX_RATE_QUOTE,
    REQUEST_TIMEOUT,
    SIGNAL_LIMIT,
    SUPPORTED_INTERVALS,
    TREND_LIMIT,
)
from storage import Storage

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
}

_PANDAS_FREQUENCIES = {
    "1m": "1min",
    "3m": "3min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
}


class HistoricalDataError(ValueError):
    """Raised when imported historical market data is invalid."""


@dataclass
class HistoricalDataset:
    dataframe: pd.DataFrame
    detected_format: str
    base_interval: str
    source_filename: str | None = None

    def save_normalized_csv(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.dataframe[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        payload["timestamp"] = payload["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload.to_csv(target, index=False)
        return target


class HistoricalDataLoader:
    GENERIC_ALIASES = {
        "timestamp": {"timestamp", "time", "datetime", "date"},
        "open": {"open"},
        "high": {"high"},
        "low": {"low"},
        "close": {"close"},
        "volume": {"volume", "vol"},
    }

    BINANCE_ALIASES = {
        "timestamp": {"open_time", "open time"},
        "open": {"open"},
        "high": {"high"},
        "low": {"low"},
        "close": {"close"},
        "volume": {"volume"},
    }

    def load_csv(
        self,
        raw_bytes: bytes,
        *,
        base_interval: str,
        trend_interval: str,
        signal_interval: str,
        source_filename: str | None = None,
    ) -> HistoricalDataset:
        if not raw_bytes:
            raise HistoricalDataError("Uploaded file is empty")
        if base_interval not in SUPPORTED_INTERVALS:
            raise HistoricalDataError(f"Unsupported base interval: {base_interval}")

        frame = pd.read_csv(BytesIO(raw_bytes))
        if frame.empty:
            raise HistoricalDataError("Uploaded CSV has no rows")

        detected_format, normalized = self._normalize_frame(frame)
        self._validate_frame(normalized, base_interval, trend_interval, signal_interval)

        enriched = normalized.copy()
        base_delta = interval_to_timedelta(base_interval)
        enriched["close_time"] = enriched["timestamp"] + base_delta

        return HistoricalDataset(
            dataframe=enriched.reset_index(drop=True),
            detected_format=detected_format,
            base_interval=base_interval,
            source_filename=source_filename,
        )

    def _normalize_frame(self, frame: pd.DataFrame) -> tuple[str, pd.DataFrame]:
        normalized_columns = {self._normalize_header(column): column for column in frame.columns}
        mappings = (
            ("binance", self.BINANCE_ALIASES),
            ("generic", self.GENERIC_ALIASES),
        )

        for detected_format, aliases in mappings:
            resolved: dict[str, str] = {}
            for field_name, candidates in aliases.items():
                for candidate in candidates:
                    column = normalized_columns.get(candidate)
                    if column is not None:
                        resolved[field_name] = column
                        break
                if field_name not in resolved:
                    break
            else:
                payload = pd.DataFrame(
                    {
                        "timestamp": self._parse_timestamp_column(frame[resolved["timestamp"]]),
                        "open": pd.to_numeric(frame[resolved["open"]], errors="coerce"),
                        "high": pd.to_numeric(frame[resolved["high"]], errors="coerce"),
                        "low": pd.to_numeric(frame[resolved["low"]], errors="coerce"),
                        "close": pd.to_numeric(frame[resolved["close"]], errors="coerce"),
                        "volume": pd.to_numeric(frame[resolved["volume"]], errors="coerce"),
                    }
                )
                return detected_format, payload

        raise HistoricalDataError(
            "CSV must match either generic OHLCV columns "
            "(timestamp, open, high, low, close, volume) "
            "or Binance kline columns (open_time, open, high, low, close, volume)"
        )

    def _parse_timestamp_column(self, series: pd.Series) -> pd.Series:
        if is_numeric_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce")
            sample = numeric.dropna()
            if sample.empty:
                return pd.to_datetime(series, errors="coerce", utc=True)
            unit = "ms" if float(sample.iloc[0]) > 10_000_000_000 else "s"
            return pd.to_datetime(numeric, errors="coerce", unit=unit, utc=True)
        return pd.to_datetime(series, errors="coerce", utc=True)

    def _validate_frame(
        self,
        frame: pd.DataFrame,
        base_interval: str,
        trend_interval: str,
        signal_interval: str,
    ) -> None:
        if frame.isnull().any().any():
            raise HistoricalDataError("CSV contains missing or unparseable OHLCV fields")

        frame.sort_values("timestamp", inplace=True)
        frame.reset_index(drop=True, inplace=True)

        if frame["timestamp"].duplicated().any():
            raise HistoricalDataError("Historical candles contain duplicate timestamps")
        if not frame["timestamp"].is_monotonic_increasing:
            raise HistoricalDataError("Historical candles are out of order")
        if (frame[["open", "high", "low", "close"]] <= 0).any().any():
            raise HistoricalDataError("Historical OHLC values must be positive")
        if (frame["volume"] < 0).any():
            raise HistoricalDataError("Historical candle volume must be non-negative")

        invalid_shape = (
            (frame["high"] < frame["low"])
            | (frame["open"] > frame["high"])
            | (frame["open"] < frame["low"])
            | (frame["close"] > frame["high"])
            | (frame["close"] < frame["low"])
        )
        if invalid_shape.any():
            raise HistoricalDataError("Historical candles contain impossible OHLC shapes")

        actual_delta = infer_interval_delta(frame["timestamp"])
        expected_delta = interval_to_timedelta(base_interval)
        if actual_delta != expected_delta:
            raise HistoricalDataError(
                f"Historical base interval mismatch: expected {base_interval}, got {timedelta_to_interval(actual_delta)}"
            )

        for target_interval in (trend_interval, signal_interval):
            target_delta = interval_to_timedelta(target_interval)
            if target_delta % expected_delta != pd.Timedelta(0):
                raise HistoricalDataError(
                    f"Base interval {base_interval} must evenly divide {target_interval}"
                )

        trend_resampled = resample_ohlcv(frame, trend_interval)
        signal_resampled = resample_ohlcv(frame, signal_interval)
        if len(trend_resampled) < TREND_LIMIT:
            raise HistoricalDataError(
                f"Historical data produces only {len(trend_resampled)} closed {trend_interval} candles; need {TREND_LIMIT}"
            )
        if len(signal_resampled) < SIGNAL_LIMIT:
            raise HistoricalDataError(
                f"Historical data produces only {len(signal_resampled)} closed {signal_interval} candles; need {SIGNAL_LIMIT}"
            )

    def _normalize_header(self, value: str) -> str:
        return value.strip().lower().replace(" ", "_")


class HistoricalFxService:
    def __init__(
        self,
        storage: Storage,
        *,
        base_url: str = FRANKFURTER_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.storage = storage
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def prefetch_dates(self, dates: Iterable[date | str]) -> None:
        for target in dates:
            self.get_rate_for_date(target)

    def get_rate_for_date(self, target: date | str) -> tuple[float, str]:
        requested_date = target.isoformat() if isinstance(target, date) else str(target)
        cached = self.storage.get_historical_fx_rate(requested_date)
        if cached is not None:
            return cached

        rate, resolved_date = self._fetch_with_fallback(requested_date)
        self.storage.cache_historical_fx_rate(requested_date, resolved_date, rate)
        return rate, resolved_date

    def _fetch_with_fallback(self, requested_date: str) -> tuple[float, str]:
        current = datetime.fromisoformat(requested_date).date()
        for _ in range(8):
            try:
                rate, resolved_date = self._fetch_single_date(current.isoformat())
                return rate, resolved_date
            except HistoricalDataError:
                current -= timedelta(days=1)
        raise HistoricalDataError(f"Failed to fetch historical FX rate for {requested_date}")

    def _fetch_single_date(self, target_date: str) -> tuple[float, str]:
        url = f"{self.base_url}/v2/{target_date}"
        try:
            response = self.session.get(
                url,
                params={"from": FX_RATE_BASE, "to": FX_RATE_QUOTE, "symbols": FX_RATE_QUOTE},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise HistoricalDataError(f"Failed to fetch historical FX for {target_date}: {exc}") from exc

        if "rate" in payload:
            rate = float(payload["rate"])
            resolved_date = str(payload.get("date", target_date))
            return rate, resolved_date

        rates = payload.get("rates")
        if isinstance(rates, dict):
            if FX_RATE_QUOTE in rates and isinstance(rates[FX_RATE_QUOTE], (int, float)):
                return float(rates[FX_RATE_QUOTE]), str(payload.get("date", target_date))
            if rates:
                first_date, first_payload = next(iter(rates.items()))
                if isinstance(first_payload, dict) and FX_RATE_QUOTE in first_payload:
                    return float(first_payload[FX_RATE_QUOTE]), str(first_date)

        raise HistoricalDataError(f"Unexpected historical FX payload for {target_date}")


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    seconds = _INTERVAL_TO_SECONDS.get(interval)
    if seconds is None:
        raise HistoricalDataError(f"Unsupported interval: {interval}")
    return pd.Timedelta(seconds=seconds)


def timedelta_to_interval(delta: pd.Timedelta) -> str:
    seconds = int(delta.total_seconds())
    for interval, interval_seconds in _INTERVAL_TO_SECONDS.items():
        if interval_seconds == seconds:
            return interval
    raise HistoricalDataError(f"Unsupported candle spacing: {seconds} seconds")


def infer_interval_delta(series: pd.Series) -> pd.Timedelta:
    cadence = series.diff().dropna()
    if cadence.empty:
        raise HistoricalDataError("Historical CSV must contain at least two candles")
    unique_cadence = cadence.unique()
    if len(unique_cadence) != 1:
        raise HistoricalDataError("Historical candles must have a regular cadence")
    return pd.Timedelta(unique_cadence[0])


def resample_ohlcv(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    rule = _PANDAS_FREQUENCIES.get(interval)
    if rule is None:
        raise HistoricalDataError(f"Unsupported interval: {interval}")

    indexed = frame.set_index("timestamp").sort_index()
    aggregated = indexed.resample(rule, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    aggregated = aggregated.dropna(subset=["open", "high", "low", "close"])
    aggregated.reset_index(inplace=True)
    aggregated["close_time"] = aggregated["timestamp"] + interval_to_timedelta(interval)
    return aggregated
