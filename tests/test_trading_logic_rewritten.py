from __future__ import annotations

import shutil
import time
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pandas as pd
from fastapi.testclient import TestClient

from historical_data import HistoricalDataError, HistoricalDataLoader, HistoricalFxService
from indicators import rsi
from live_market_data import DataIntegrityError, MarketDataFetcher
from market_sources import HistoricalPlaybackSource
from portfolio import Portfolio
from run_manager import RunManager
from storage import Storage
from strategy import StrategyEngine
from web_app import create_app
from web_models import SimulationConfig


def make_workspace_tmpdir() -> Path:
    path = Path.cwd() / ".tmp-tests" / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_history_frame(periods: int = 900) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01T00:00:00Z", periods=periods, freq="15min", tz="UTC")
    opens = pd.Series(range(periods), dtype=float).mul(5).add(10000.0)
    closes = opens.add(pd.Series([(-1) ** index for index in range(periods)], dtype=float))
    highs = pd.concat([opens, closes], axis=1).max(axis=1).add(2.0)
    lows = pd.concat([opens, closes], axis=1).min(axis=1).sub(2.0)
    volumes = pd.Series(range(periods), dtype=float).add(1000.0)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def make_generic_history_csv(periods: int = 900) -> bytes:
    frame = make_history_frame(periods)
    payload = frame.copy()
    payload["timestamp"] = payload["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return payload.to_csv(index=False).encode("utf-8")


def make_binance_history_csv(periods: int = 900) -> bytes:
    frame = make_history_frame(periods)
    payload = pd.DataFrame(
        {
            "open_time": (frame["timestamp"].astype("int64") // 1_000_000).astype("int64"),
            "open": frame["open"],
            "high": frame["high"],
            "low": frame["low"],
            "close": frame["close"],
            "volume": frame["volume"],
        }
    )
    return payload.to_csv(index=False).encode("utf-8")


class IndicatorTests(unittest.TestCase):
    def test_rsi_returns_100_for_persistent_gains(self) -> None:
        series = pd.Series(range(1, 17), dtype=float)
        self.assertEqual(rsi(series).iloc[-1], 100.0)

    def test_rsi_returns_50_for_flat_prices(self) -> None:
        series = pd.Series([10.0] * 20)
        self.assertEqual(rsi(series).iloc[-1], 50.0)


class PortfolioTests(unittest.TestCase):
    def test_sell_clears_entry_state_and_unrealized_pct(self) -> None:
        portfolio = Portfolio(1000)
        portfolio.execute_buy(100, "taker", "test")
        portfolio.execute_sell(110, "maker", "take profit")
        self.assertEqual(portfolio.entry_price, 0.0)
        self.assertEqual(portfolio.get_unrealized_pnl_pct(120), 0.0)

    def test_mark_to_market_preserves_intratrade_drawdown_peak(self) -> None:
        portfolio = Portfolio(1000)
        portfolio.execute_buy(100, "taker", "test")
        portfolio.mark_to_market(120)
        self.assertAlmostEqual(portfolio.get_max_drawdown(110), 8.3333333333, places=6)


class StrategyTests(unittest.TestCase):
    def test_stop_loss_uses_runtime_config(self) -> None:
        portfolio = Portfolio(1000)
        portfolio.execute_buy(100, "taker", "test")
        engine = StrategyEngine(SimulationConfig(stop_loss_pct=0.01))
        engine.on_entry(100)
        price = 98.15
        portfolio_value = portfolio.get_total_value(price)
        signal = engine.evaluate(
            price,
            {"rsi": 50, "ema200": 0, "ema20": 100, "macd_hist": 1, "macd_hist_prev": 1},
            portfolio_value,
            portfolio.starting_capital,
            portfolio.entry_price * portfolio.btc_held,
        )
        self.assertEqual(signal.action, "SELL")

    def test_stop_loss_is_based_on_entry_cost_not_starting_capital(self) -> None:
        portfolio = Portfolio(1000)
        portfolio.execute_buy(100, "taker", "test")
        engine = StrategyEngine(SimulationConfig(stop_loss_pct=0.013))
        engine.on_entry(100)
        signal = engine.evaluate(
            98.8,
            {"rsi": 50, "ema200": 0, "ema20": 100, "macd_hist": 1, "macd_hist_prev": 1},
            portfolio.get_total_value(98.8),
            portfolio.starting_capital,
            portfolio.entry_price * portfolio.btc_held,
        )
        self.assertEqual(signal.action, "HOLD")

    def test_soft_sell_requires_min_profit_threshold(self) -> None:
        portfolio = Portfolio(1000)
        portfolio.execute_buy(100, "taker", "test")
        engine = StrategyEngine(SimulationConfig(soft_sell_min_profit_pct=0.03))
        engine.on_entry(100)

        hold_signal = engine.evaluate(
            101,
            {"rsi": 80, "ema200": 0, "ema20": 100, "macd_hist": 1, "macd_hist_prev": 1},
            portfolio.get_total_value(101),
            portfolio.starting_capital,
            portfolio.entry_price * portfolio.btc_held,
        )
        self.assertEqual(hold_signal.action, "HOLD")
        self.assertIn("Soft sell deferred", hold_signal.reason)

        sell_signal = engine.evaluate(
            103.5,
            {"rsi": 80, "ema200": 0, "ema20": 100, "macd_hist": 1, "macd_hist_prev": 1},
            portfolio.get_total_value(103.5),
            portfolio.starting_capital,
            portfolio.entry_price * portfolio.btc_held,
        )
        self.assertEqual(sell_signal.action, "SELL")

    def test_exit_cooldown_blocks_reentry_until_elapsed(self) -> None:
        engine = StrategyEngine(SimulationConfig(exit_cooldown_minutes=30))
        exit_time = datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc)
        engine.on_exit(exit_time)

        hold_signal = engine.evaluate(
            100,
            {"rsi": 60, "ema200": 90, "ema20": 99, "macd_hist": 1, "macd_hist_prev": 1},
            1000,
            1000,
            0,
            exit_time + timedelta(minutes=10),
        )
        self.assertEqual(hold_signal.action, "HOLD")
        self.assertIn("Exit cooldown active", hold_signal.reason)

        buy_signal = engine.evaluate(
            100,
            {"rsi": 60, "ema200": 90, "ema20": 99, "macd_hist": 1, "macd_hist_prev": 1},
            1000,
            1000,
            0,
            exit_time + timedelta(minutes=31),
        )
        self.assertEqual(buy_signal.action, "BUY")


class ConfigTests(unittest.TestCase):
    def test_simulation_config_defaults(self) -> None:
        config = SimulationConfig()
        self.assertEqual(config.symbol, "BTCUSDT")
        self.assertEqual(config.data_source, "live")
        self.assertGreater(config.starting_capital_twd, 0)
        self.assertEqual(config.soft_sell_min_profit_pct, 0.0)
        self.assertEqual(config.exit_cooldown_minutes, 0.0)

    def test_simulation_config_requires_historical_interval(self) -> None:
        with self.assertRaises(ValueError):
            SimulationConfig(data_source="historical")


class MarketDataFetcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fetcher = MarketDataFetcher()

    def test_sanitize_klines_uses_only_closed_candles(self) -> None:
        df = pd.DataFrame(
            {
                "open_time": pd.to_datetime(["2026-04-08T10:00:00Z", "2026-04-08T10:15:00Z", "2026-04-08T10:30:00Z"], utc=True),
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "volume": [10.0, 11.0, 12.0],
                "close_time": pd.to_datetime(
                    ["2026-04-08T10:14:59.999Z", "2026-04-08T10:29:59.999Z", "2026-04-08T10:44:59.999Z"],
                    utc=True,
                ),
            }
        )
        result = self.fetcher._sanitize_klines(df, interval="15m", limit=2, now=pd.Timestamp("2026-04-08T10:35:00Z"))
        self.assertEqual(result["close"].tolist(), [100.5, 101.5])

    def test_sanitize_klines_rejects_duplicate_timestamps(self) -> None:
        df = pd.DataFrame(
            {
                "open_time": pd.to_datetime(["2026-04-08T10:00:00Z", "2026-04-08T10:00:00Z"], utc=True),
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [10.0, 11.0],
                "close_time": pd.to_datetime(["2026-04-08T10:14:59.999Z", "2026-04-08T10:29:59.999Z"], utc=True),
            }
        )
        with self.assertRaises(DataIntegrityError):
            self.fetcher._sanitize_klines(df, interval="15m", limit=2, now=pd.Timestamp("2026-04-08T10:35:00Z"))


class HistoricalLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = HistoricalDataLoader()

    def test_loader_accepts_generic_ohlcv_csv(self) -> None:
        dataset = self.loader.load_csv(
            make_generic_history_csv(),
            base_interval="15m",
            trend_interval="1h",
            signal_interval="15m",
            source_filename="generic.csv",
        )
        self.assertEqual(dataset.detected_format, "generic")
        self.assertEqual(dataset.base_interval, "15m")
        self.assertIn("close_time", dataset.dataframe.columns)

    def test_loader_accepts_binance_csv(self) -> None:
        dataset = self.loader.load_csv(
            make_binance_history_csv(),
            base_interval="15m",
            trend_interval="1h",
            signal_interval="15m",
            source_filename="binance.csv",
        )
        self.assertEqual(dataset.detected_format, "binance")
        self.assertEqual(len(dataset.dataframe), 900)

    def test_loader_rejects_irregular_cadence(self) -> None:
        frame = make_history_frame(300)
        frame = frame.drop(index=[10]).reset_index(drop=True)
        payload = frame.copy()
        payload["timestamp"] = payload["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaises(HistoricalDataError):
            self.loader.load_csv(
                payload.to_csv(index=False).encode("utf-8"),
                base_interval="15m",
                trend_interval="1h",
                signal_interval="15m",
            )


class StubHistoricalFxService(HistoricalFxService):
    def __init__(self, storage: Storage, *, weekend_fallback: bool = False) -> None:
        super().__init__(storage)
        self.calls: list[str] = []
        self.weekend_fallback = weekend_fallback

    def _fetch_single_date(self, target_date: str) -> tuple[float, str]:
        self.calls.append(target_date)
        if self.weekend_fallback and target_date == "2026-04-04":
            raise HistoricalDataError("Weekend")
        return 32.0 + (int(target_date[-2:]) % 5), target_date


class HistoricalFxServiceTests(unittest.TestCase):
    def test_service_caches_and_falls_back_to_previous_date(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            storage = Storage(tmpdir / "autobit.db")
            service = StubHistoricalFxService(storage, weekend_fallback=True)
            rate, resolved_date = service.get_rate_for_date("2026-04-04")
            self.assertEqual(resolved_date, "2026-04-03")
            self.assertEqual(storage.get_historical_fx_rate("2026-04-04"), (rate, resolved_date))

            service.calls.clear()
            cached_rate, cached_date = service.get_rate_for_date("2026-04-04")
            self.assertEqual((cached_rate, cached_date), (rate, resolved_date))
            self.assertEqual(service.calls, [])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class HistoricalPlaybackTests(unittest.TestCase):
    def test_playback_exposes_market_timestamp_and_warmup_window(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            storage = Storage(tmpdir / "autobit.db")
            dataset = HistoricalDataLoader().load_csv(
                make_generic_history_csv(),
                base_interval="15m",
                trend_interval="1h",
                signal_interval="15m",
            )
            fx_service = StubHistoricalFxService(storage)
            source = HistoricalPlaybackSource(
                dataset.dataframe,
                base_interval="15m",
                config=SimulationConfig(
                    data_source="historical",
                    historical_base_interval="15m",
                    trend_interval="1h",
                    signal_interval="15m",
                ),
                fx_service=fx_service,
            )
            snapshot = source.get_next_snapshot()
            self.assertEqual(len(snapshot.trend_df), 220)
            self.assertEqual(len(snapshot.signal_df), 100)
            self.assertIsNotNone(snapshot.market_timestamp)
            self.assertEqual(snapshot.playback_index, 1)
            self.assertGreater(snapshot.playback_total or 0, 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class FakeFetcher:
    def __init__(self) -> None:
        self.price_index = 0
        self.prices = [100.0, 102.0, 104.0, 103.0]

    def get_display_fx_rate(self):
        return 32.0, "2026-04-08"

    def get_current_price(self, symbol: str = "BTCUSDT") -> float:
        price = self.prices[min(self.price_index, len(self.prices) - 1)]
        self.price_index += 1
        return price

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        base = 90 if interval == "1h" else 95
        close = [base + i * 0.2 for i in range(limit)]
        return pd.DataFrame(
            {
                "open_time": pd.date_range("2026-04-08", periods=limit, freq="15min", tz="UTC"),
                "open": close,
                "high": [value + 1 for value in close],
                "low": [value - 1 for value in close],
                "close": close,
                "volume": [1000.0] * limit,
            }
        )


class RunManagerTests(unittest.TestCase):
    def test_only_one_active_run_is_allowed(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            storage = Storage(tmpdir / "autobit.db")
            manager = RunManager(storage, fetcher_factory=FakeFetcher)
            first_run = manager.start_run(SimulationConfig(check_interval_sec=0.05))
            self.assertEqual(first_run.status, "running")
            with self.assertRaises(RuntimeError):
                manager.start_run(SimulationConfig(check_interval_sec=0.05))
            manager.stop_run(first_run.id)
            time.sleep(0.1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class ApiTests(unittest.TestCase):
    def test_api_can_create_query_and_stop_live_run(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = create_app(db_path=db_path, run_manager=RunManager(Storage(db_path), fetcher_factory=FakeFetcher))
            client = TestClient(app)

            response = client.post("/api/runs", json={"starting_capital_twd": 10000, "check_interval_sec": 0.05})
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["id"]
            time.sleep(0.15)

            self.assertEqual(client.get("/api/runs").status_code, 200)
            detail = client.get(f"/api/runs/{run_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertGreaterEqual(len(detail.json()["ticks"]), 1)
            self.assertEqual(client.post(f"/api/runs/{run_id}/stop").status_code, 200)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_historical_run_endpoint_uploads_and_completes(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = create_app(
                db_path=db_path,
                run_manager=RunManager(
                    Storage(db_path),
                    fetcher_factory=FakeFetcher,
                    historical_fx_factory=lambda storage: StubHistoricalFxService(storage),
                    historical_data_dir=tmpdir / "historical",
                ),
            )
            client = TestClient(app)

            response = client.post(
                "/api/runs/historical",
                data={
                    "starting_capital_twd": "10000",
                    "check_interval_sec": "0.05",
                    "symbol": "BTCUSDT",
                    "trend_interval": "1h",
                    "signal_interval": "15m",
                    "rsi_entry_low": "50",
                    "rsi_entry_high": "70",
                    "rsi_exit_high": "75",
                    "anti_chase_pct": "0.02",
                    "stop_loss_pct": "0.02",
                    "trail_trigger_pct": "0.015",
                    "trail_stop_pct": "0.01",
                    "historical_base_interval": "15m",
                },
                files={"file": ("history.csv", BytesIO(make_generic_history_csv()), "text/csv")},
            )
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["id"]

            time.sleep(1.4)
            detail = client.get(f"/api/runs/{run_id}")
            self.assertEqual(detail.status_code, 200)
            payload = detail.json()
            self.assertEqual(payload["run"]["config"]["data_source"], "historical")
            self.assertEqual(payload["run"]["status"], "completed")
            self.assertGreaterEqual(len(payload["ticks"]), 1)
            self.assertIsNotNone(payload["ticks"][0]["market_timestamp"])
            self.assertTrue((tmpdir / "historical" / f"{run_id}.csv").exists())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_log_import_endpoint_imports_legacy_file(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            log_path = tmpdir / "autobit.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-04-02 12:25:57 [INFO] Simulation loop started | capital 10000 TWD | 312.5 USDT",
                        "2026-04-02 12:25:57 [INFO] Tick started | tick=1",
                        "2026-04-02 12:25:58 [INFO] Indicators | EMA200=68075.30 EMA20=67168.74 RSI=26.47 MACD_H=-75.218351",
                        "2026-04-02 12:25:58 [INFO] Signal | HOLD | Trend filter failed",
                        "2026-04-02 12:26:11 [INFO] Simulation stopped",
                    ]
                ),
                encoding="utf-8",
            )
            app = create_app(
                db_path=db_path,
                run_manager=RunManager(Storage(db_path), fetcher_factory=FakeFetcher, log_path=log_path),
            )
            client = TestClient(app)
            response = client.post("/api/import/log", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["imported_runs"], 1)
            self.assertEqual(len(client.get("/api/runs").json()), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
