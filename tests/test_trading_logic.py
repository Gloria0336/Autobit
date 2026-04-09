from __future__ import annotations

import shutil
import time
import unittest
from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd
from fastapi.testclient import TestClient

from analysis_ai import ReportAnalysisService
from analysis_report import RunReportService
from historical_data import HistoricalBinanceFetcher, HistoricalDataError, HistoricalDataLoader, HistoricalFxService
from indicators import rsi
from live_market_data import DataIntegrityError, MarketDataFetcher
from market_sources import HistoricalPlaybackSource
from openrouter_client import OpenRouterConfigurationError, OpenRouterResult
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


def load_config_module():
    import importlib
    import config

    return importlib.reload(config)


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


def make_binance_api_klines(periods: int = 900) -> list[list[object]]:
    frame = make_history_frame(periods)
    interval_ms = 15 * 60 * 1000
    payload: list[list[object]] = []
    for row in frame.itertuples(index=False):
        open_ms = int(pd.Timestamp(row.timestamp).timestamp() * 1000)
        payload.append(
            [
                open_ms,
                f"{row.open:.8f}",
                f"{row.high:.8f}",
                f"{row.low:.8f}",
                f"{row.close:.8f}",
                f"{row.volume:.8f}",
                open_ms + interval_ms - 1,
                "0",
                0,
                "0",
                "0",
                "0",
            ]
        )
    return payload


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


class ConfigTests(unittest.TestCase):
    def test_simulation_config_defaults(self) -> None:
        config = SimulationConfig()
        self.assertEqual(config.symbol, "BTCUSDT")
        self.assertEqual(config.data_source, "live")
        self.assertGreater(config.starting_capital_twd, 0)
        self.assertEqual(config.check_interval_sec, 10.0)

    def test_simulation_config_requires_historical_interval(self) -> None:
        with self.assertRaises(ValueError):
            SimulationConfig(data_source="historical")

    def test_simulation_config_allows_csv_historical_without_dates(self) -> None:
        config = SimulationConfig(
            data_source="historical",
            historical_source_mode="csv_upload",
            historical_base_interval="15m",
        )
        self.assertEqual(config.historical_source_mode, "csv_upload")

    def test_config_can_load_openrouter_values_from_env_file(self) -> None:
        env_path = Path.cwd() / ".env"
        original = env_path.read_text(encoding="utf-8") if env_path.exists() else None
        original_api_key = os.environ.pop("OPENROUTER_API_KEY", None)
        original_model = os.environ.pop("OPENROUTER_MODEL", None)
        try:
            env_path.write_text(
                "\n".join(
                    [
                        "OPENROUTER_API_KEY=test-key-from-env-file",
                        "OPENROUTER_MODEL=openai/gpt-4.1-mini",
                    ]
                ),
                encoding="utf-8",
            )
            config_module = load_config_module()
            self.assertEqual(config_module.OPENROUTER_API_KEY, "test-key-from-env-file")
            self.assertEqual(config_module.OPENROUTER_MODEL, "openai/gpt-4.1-mini")
        finally:
            if original is None:
                env_path.unlink(missing_ok=True)
            else:
                env_path.write_text(original, encoding="utf-8")
            if original_api_key is not None:
                os.environ["OPENROUTER_API_KEY"] = original_api_key
            else:
                os.environ.pop("OPENROUTER_API_KEY", None)
            if original_model is not None:
                os.environ["OPENROUTER_MODEL"] = original_model
            else:
                os.environ.pop("OPENROUTER_MODEL", None)
            load_config_module()


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


class StubResponse:
    def __init__(self, payload: list[list[object]]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[list[object]]:
        return self.payload


class StubKlineSession:
    def __init__(self, pages: dict[int, list[list[object]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, object] | None = None, timeout: int | None = None) -> StubResponse:
        params = params or {}
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        return StubResponse(self.pages.get(int(params["startTime"]), []))


class HistoricalBinanceFetcherTests(unittest.TestCase):
    def test_fetch_dataset_paginates_binance_klines(self) -> None:
        raw = make_binance_api_klines(1001)
        frame = make_history_frame(1001)
        start_at = frame["timestamp"].iloc[0].to_pydatetime()
        end_at = (frame["timestamp"].iloc[-1] + pd.Timedelta(minutes=15)).to_pydatetime()
        second_page_start = int(raw[1000][0])
        session = StubKlineSession(
            {
                int(raw[0][0]): raw[:1000],
                second_page_start: raw[1000:],
            }
        )
        fetcher = HistoricalBinanceFetcher(session=session)

        dataset = fetcher.fetch_dataset(
            symbol="BTCUSDT",
            base_interval="15m",
            trend_interval="1h",
            signal_interval="15m",
            start_at=start_at,
            end_at=end_at,
        )

        self.assertEqual(len(dataset.dataframe), 1001)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(dataset.detected_format, "binance_api")


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


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class RecordingSession:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, params: dict[str, object] | None = None, timeout: int | None = None) -> FakeResponse:
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout})
        return FakeResponse(self.payload)


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

    def test_service_uses_frankfurter_v2_pair_endpoint_for_historical_dates(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            storage = Storage(tmpdir / "autobit.db")
            session = RecordingSession({"date": "2026-03-19", "amount": 1.0, "base": "USD", "quote": "TWD", "rate": 32.91})
            service = HistoricalFxService(storage, session=session)

            rate, resolved_date = service.get_rate_for_date("2026-03-19")

            self.assertEqual((rate, resolved_date), (32.91, "2026-03-19"))
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(session.calls[0]["url"], "https://api.frankfurter.dev/v2/rate/USD/TWD")
            self.assertEqual(session.calls[0]["params"], {"date": "2026-03-19"})
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


class FakeHistoricalBinanceFetcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def fetch_dataset(
        self,
        *,
        symbol: str,
        base_interval: str,
        trend_interval: str,
        signal_interval: str,
        start_at,
        end_at,
        source_filename: str | None = None,
    ):
        self.calls.append(
            {
                "symbol": symbol,
                "base_interval": base_interval,
                "trend_interval": trend_interval,
                "signal_interval": signal_interval,
                "start_at": start_at,
                "end_at": end_at,
                "source_filename": source_filename,
            }
        )
        return HistoricalDataLoader().load_csv(
            make_generic_history_csv(),
            base_interval=base_interval,
            trend_interval=trend_interval,
            signal_interval=signal_interval,
            source_filename=source_filename or f"binance:{symbol}:{base_interval}",
        )


class FakeOpenRouterSuccessClient:
    def analyze(
        self,
        *,
        prompt: str,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        del prompt, api_key, referer, title
        return OpenRouterResult(
            model=model,
            content="\n".join(
                [
                    "# AI Analysis",
                    "",
                    "Entry filters look too strict and should be loosened slightly.",
                    "",
                    "```json",
                    """{
  "summary": "Entry filters look too strict.",
  "observations": [
    {"title": "Low trade count", "detail": "There are too few round trips.", "severity": "warning"}
  ],
  "recommendations": [
    {
      "parameter": "rsi_entry_low",
      "current_value": 50,
      "suggested_change": "decrease",
      "suggested_value": 47,
      "reason": "Lowering the floor should admit more setups.",
      "expected_effect": "Increase trade count without fully removing RSI filtering.",
      "confidence": "medium"
    }
  ],
  "test_plan": [
    "Compare current RSI entry floor with 47 over the same historical window."
  ]
}""",
                    "```",
                ]
            ),
        )

    def test_connection(
        self,
        *,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        del api_key, referer, title
        return OpenRouterResult(model=model, content="OpenRouter connection OK.")


class FakeOpenRouterMalformedClient:
    def analyze(
        self,
        *,
        prompt: str,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        del prompt, api_key, referer, title
        return OpenRouterResult(model=model, content="# AI Analysis\n\nThis response forgot the JSON block.")


class FakeOpenRouterMissingConfigClient:
    def analyze(
        self,
        *,
        prompt: str,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        del prompt, model, api_key, referer, title
        raise OpenRouterConfigurationError("OPENROUTER_API_KEY is not configured")

    def test_connection(
        self,
        *,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        del model, api_key, referer, title
        raise OpenRouterConfigurationError("OPENROUTER_API_KEY is not configured")


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
    def _create_historical_app(
        self,
        db_path: Path,
        tmpdir: Path,
        historical_binance_factory=None,
    ):
        return create_app(
            db_path=db_path,
            run_manager=RunManager(
                Storage(db_path),
                fetcher_factory=FakeFetcher,
                historical_fx_factory=lambda storage: StubHistoricalFxService(storage),
                historical_binance_factory=historical_binance_factory,
                historical_data_dir=tmpdir / "historical",
            ),
        )

    def _create_completed_historical_run(self, client: TestClient) -> tuple[str, dict]:
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

        payload = None
        for _ in range(60):
            detail = client.get(f"/api/runs/{run_id}")
            self.assertEqual(detail.status_code, 200)
            payload = detail.json()
            if payload["run"]["status"] == "completed":
                break
            time.sleep(0.1)
        self.assertIsNotNone(payload)
        return run_id, payload

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
            app = self._create_historical_app(db_path, tmpdir)
            client = TestClient(app)

            run_id, payload = self._create_completed_historical_run(client)
            self.assertEqual(payload["run"]["config"]["data_source"], "historical")
            self.assertEqual(payload["run"]["status"], "completed")
            self.assertGreaterEqual(len(payload["ticks"]), 1)
            self.assertIsNotNone(payload["ticks"][0]["market_timestamp"])
            self.assertTrue((tmpdir / "historical" / f"{run_id}.csv").exists())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_historical_run_endpoint_fetches_from_binance_api_and_completes(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            fake_historical_fetcher = FakeHistoricalBinanceFetcher()
            app = self._create_historical_app(
                db_path,
                tmpdir,
                historical_binance_factory=lambda: fake_historical_fetcher,
            )
            client = TestClient(app)

            response = client.post(
                "/api/runs/historical",
                files=[
                    ("starting_capital_twd", (None, "10000")),
                    ("check_interval_sec", (None, "0.05")),
                    ("symbol", (None, "BTCUSDT")),
                    ("trend_interval", (None, "1h")),
                    ("signal_interval", (None, "15m")),
                    ("rsi_entry_low", (None, "50")),
                    ("rsi_entry_high", (None, "70")),
                    ("rsi_exit_high", (None, "75")),
                    ("anti_chase_pct", (None, "0.02")),
                    ("stop_loss_pct", (None, "0.02")),
                    ("trail_trigger_pct", (None, "0.015")),
                    ("trail_stop_pct", (None, "0.01")),
                    ("historical_source_mode", (None, "binance_api")),
                    ("historical_base_interval", (None, "15m")),
                    ("historical_start_at", (None, "2026-01-01T00:00:00Z")),
                    ("historical_end_at", (None, "2026-01-10T09:00:00Z")),
                ],
            )
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["id"]

            payload = None
            for _ in range(60):
                detail = client.get(f"/api/runs/{run_id}")
                self.assertEqual(detail.status_code, 200)
                payload = detail.json()
                if payload["run"]["status"] == "completed":
                    break
                time.sleep(0.1)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["run"]["status"], "completed")
            self.assertEqual(payload["run"]["config"]["historical_source_mode"], "binance_api")
            self.assertEqual(len(fake_historical_fetcher.calls), 1)
            self.assertTrue((tmpdir / "historical" / f"{run_id}.csv").exists())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_report_endpoint_returns_json_markdown_and_prompt(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = self._create_historical_app(db_path, tmpdir)
            client = TestClient(app)
            run_id, _ = self._create_completed_historical_run(client)

            response = client.get(f"/api/runs/{run_id}/report")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["report"]["run_context"]["run_id"], run_id)
            self.assertIn("Autobit Run Analysis Report", payload["markdown"])
            self.assertIn("Autobit Run Analysis Report", payload["prompt"])
            self.assertIn("performance", payload["report"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_analysis_config_endpoint_returns_model_candidates(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = create_app(db_path=db_path, run_manager=RunManager(Storage(db_path), fetcher_factory=FakeFetcher))
            client = TestClient(app)
            response = client.get("/api/config/analysis")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["provider"], "openrouter")
            self.assertGreater(len(payload["recommended_models"]), 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_report_markdown_endpoint_returns_plain_markdown(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = self._create_historical_app(db_path, tmpdir)
            client = TestClient(app)
            run_id, _ = self._create_completed_historical_run(client)

            response = client.get(f"/api/runs/{run_id}/report.md")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Autobit Run Analysis Report", response.text)
            self.assertIn("Strategy Parameters", response.text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_report_endpoint_handles_run_without_trades(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            storage = Storage(db_path)
            storage.create_run(
                "no-trade-run",
                "completed",
                "2026-04-08T00:00:00Z",
                SimulationConfig(data_source="live").model_dump(),
            )
            storage.update_run(
                "no-trade-run",
                ended_at="2026-04-08T00:05:00Z",
                summary={
                    "starting_capital_twd": 10000,
                    "current_value_twd": 10000,
                    "pnl_twd": 0,
                    "pnl_pct": 0,
                    "max_drawdown_pct": 0,
                    "total_fee_twd": 0,
                    "win_rate_pct": 0,
                    "trade_count": 0,
                    "last_price_twd": None,
                    "latest_signal": None,
                },
            )
            app = create_app(db_path=db_path, run_manager=RunManager(storage, fetcher_factory=FakeFetcher))
            client = TestClient(app)

            response = client.get("/api/runs/no-trade-run/report")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["report"]["performance"]["trade_count"], 0)
            self.assertEqual(payload["report"]["trade_breakdown"]["round_trip_count"], 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_report_endpoint_returns_404_for_unknown_run(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = create_app(db_path=db_path, run_manager=RunManager(Storage(db_path), fetcher_factory=FakeFetcher))
            client = TestClient(app)
            response = client.get("/api/runs/missing/report")
            self.assertEqual(response.status_code, 404)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_analyze_report_endpoint_returns_structured_recommendations(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = self._create_historical_app(db_path, tmpdir)
            app.state.openrouter_client = FakeOpenRouterSuccessClient()
            app.state.report_analysis_service = ReportAnalysisService(
                report_service=RunReportService(),
                openrouter_client=app.state.openrouter_client,
                default_model="openai/gpt-4.1-mini",
            )
            client = TestClient(app)
            run_id, _ = self._create_completed_historical_run(client)

            response = client.post(f"/api/runs/{run_id}/report/analyze", json={})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["model"], "openai/gpt-4.1-mini")
            self.assertEqual(payload["recommendations"][0]["parameter"], "rsi_entry_low")
            self.assertEqual(payload["recommendations"][0]["suggested_value"], 47)
            self.assertEqual(len(payload["test_plan"]), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_analyze_report_endpoint_keeps_markdown_when_json_is_missing(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = self._create_historical_app(db_path, tmpdir)
            app.state.openrouter_client = FakeOpenRouterMalformedClient()
            app.state.report_analysis_service = ReportAnalysisService(
                report_service=RunReportService(),
                openrouter_client=app.state.openrouter_client,
                default_model="openai/gpt-4.1-mini",
            )
            client = TestClient(app)
            run_id, _ = self._create_completed_historical_run(client)

            response = client.post(f"/api/runs/{run_id}/report/analyze", json={})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["recommendations"], [])
            self.assertIn("forgot the JSON block", payload["ai_analysis_markdown"])
            self.assertIsNotNone(payload["parsing_error"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_analyze_report_endpoint_returns_clear_configuration_error(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = self._create_historical_app(db_path, tmpdir)
            app.state.openrouter_client = FakeOpenRouterMissingConfigClient()
            app.state.report_analysis_service = ReportAnalysisService(
                report_service=RunReportService(),
                openrouter_client=app.state.openrouter_client,
                default_model="openai/gpt-4.1-mini",
            )
            client = TestClient(app)
            run_id, _ = self._create_completed_historical_run(client)

            analyze_response = client.post(f"/api/runs/{run_id}/report/analyze", json={})
            report_response = client.get(f"/api/runs/{run_id}/report")
            self.assertEqual(analyze_response.status_code, 503)
            self.assertIn("OPENROUTER_API_KEY", analyze_response.json()["detail"])
            self.assertEqual(report_response.status_code, 200)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_analysis_test_endpoint_returns_success_message(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = create_app(db_path=db_path, run_manager=RunManager(Storage(db_path), fetcher_factory=FakeFetcher))
            app.state.openrouter_client = FakeOpenRouterSuccessClient()
            app.state.report_analysis_service.openrouter_client = app.state.openrouter_client
            client = TestClient(app)
            response = client.post("/api/analysis/test", json={"api_key": "test-key", "model": "openai/gpt-4.1-mini"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["model"], "openai/gpt-4.1-mini")
            self.assertIn("OpenRouter connection OK", payload["message"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_index_uses_new_report_dashboard_assets(self) -> None:
        tmpdir = make_workspace_tmpdir()
        try:
            db_path = tmpdir / "autobit.db"
            app = create_app(db_path=db_path, run_manager=RunManager(Storage(db_path), fetcher_factory=FakeFetcher))
            client = TestClient(app)
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("AI 分析報告", response.text)
            self.assertIn("report-api-key-input", response.text)
            self.assertIn("/assets/report_app.js", response.text)
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

