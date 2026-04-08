from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from config import HISTORICAL_DATA_DIR, LOG_FILE
from historical_data import HistoricalDataLoader, HistoricalFxService
from live_market_data import MarketDataFetcher
from log_importer import LegacyLogImporter
from market_sources import HistoricalPlaybackSource, LiveMarketDataSource
from portfolio import Portfolio
from simulator import Simulator
from storage import Storage
from strategy import StrategyEngine
from web_models import RunSummary, RunSummaryMetrics, SimulationConfig

FetcherFactory = Callable[[], MarketDataFetcher]
HistoricalFxFactory = Callable[[Storage], HistoricalFxService]


@dataclass
class ActiveRun:
    run_id: str
    config: SimulationConfig
    portfolio: Portfolio
    strategy: StrategyEngine
    simulator: Simulator
    last_price: float
    last_fx_rate: float
    last_signal: str | None = None


class RunManager:
    def __init__(
        self,
        storage: Storage,
        *,
        fetcher_factory: FetcherFactory | None = None,
        historical_fx_factory: HistoricalFxFactory | None = None,
        historical_loader: HistoricalDataLoader | None = None,
        historical_data_dir: str | Path = HISTORICAL_DATA_DIR,
        log_path: str | Path = LOG_FILE,
    ) -> None:
        self.storage = storage
        self.fetcher_factory = fetcher_factory or MarketDataFetcher
        self.historical_fx_factory = historical_fx_factory or (lambda storage: HistoricalFxService(storage))
        self.historical_loader = historical_loader or HistoricalDataLoader()
        self.historical_data_dir = Path(historical_data_dir)
        self.log_path = Path(log_path)
        self._lock = threading.Lock()
        self._active_run: ActiveRun | None = None
        self._subscribers: set[queue.Queue] = set()

    def start_run(self, config: SimulationConfig) -> RunSummary:
        live_config = config.model_copy(update={"data_source": "live"})
        with self._lock:
            if self._active_run is not None:
                raise RuntimeError("A simulation is already running")

            fetcher = self.fetcher_factory()
            fx_rate, _ = fetcher.get_display_fx_rate()
            run_id = uuid4().hex
            started_at = datetime.now(timezone.utc).isoformat()
            starting_capital_usdt = live_config.starting_capital_twd / fx_rate

            portfolio = Portfolio(starting_capital_usdt)
            strategy = StrategyEngine(live_config)
            simulator = Simulator(
                portfolio,
                LiveMarketDataSource(fetcher, live_config),
                strategy,
                display=None,
                interval_sec=live_config.check_interval_sec,
                config=live_config,
                event_callback=lambda event_type, payload: self._handle_event(run_id, event_type, payload),
                run_id=run_id,
            )

            self._create_run(run_id, started_at, live_config, portfolio, strategy, simulator, fx_rate)

        self._broadcast("run_started", {"run_id": run_id, "config": live_config.model_dump(), "started_at": started_at})
        simulator.start()
        return self._require_run(run_id)

    def start_historical_run(self, config: SimulationConfig, raw_bytes: bytes) -> RunSummary:
        historical_config = config.model_copy(update={"data_source": "historical"})
        with self._lock:
            if self._active_run is not None:
                raise RuntimeError("A simulation is already running")

            run_id = uuid4().hex
            dataset = self.historical_loader.load_csv(
                raw_bytes,
                base_interval=historical_config.historical_base_interval or "",
                trend_interval=historical_config.trend_interval,
                signal_interval=historical_config.signal_interval,
                source_filename=historical_config.historical_source_filename,
            )
            self.historical_data_dir.mkdir(parents=True, exist_ok=True)
            dataset.save_normalized_csv(self.historical_data_dir / f"{run_id}.csv")

            fx_service = self.historical_fx_factory(self.storage)
            playback_source = HistoricalPlaybackSource(
                dataset.dataframe,
                base_interval=dataset.base_interval,
                config=historical_config,
                fx_service=fx_service,
            )
            unique_dates = sorted(set(playback_source.get_market_dates()))
            fx_service.prefetch_dates(unique_dates)
            first_fx_rate = fx_service.get_rate_for_date(unique_dates[0])[0]

            started_at = datetime.now(timezone.utc).isoformat()
            starting_capital_usdt = historical_config.starting_capital_twd / first_fx_rate
            portfolio = Portfolio(starting_capital_usdt)
            strategy = StrategyEngine(historical_config)
            simulator = Simulator(
                portfolio,
                playback_source,
                strategy,
                display=None,
                interval_sec=historical_config.check_interval_sec,
                config=historical_config,
                event_callback=lambda event_type, payload: self._handle_event(run_id, event_type, payload),
                run_id=run_id,
            )

            self._create_run(run_id, started_at, historical_config, portfolio, strategy, simulator, first_fx_rate)

        self._broadcast(
            "run_started",
            {"run_id": run_id, "config": historical_config.model_dump(), "started_at": started_at},
        )
        simulator.start()
        return self._require_run(run_id)

    def stop_run(self, run_id: str) -> RunSummary:
        with self._lock:
            if self._active_run is None or self._active_run.run_id != run_id:
                run = self.storage.get_run(run_id)
                if run is None:
                    raise KeyError(run_id)
                return run
            self._active_run.simulator.stop("stopped")
        return self._require_run(run_id)

    def import_log(self, path: str | None = None) -> int:
        importer = LegacyLogImporter()
        runs = importer.parse(path or self.log_path)
        for imported in runs:
            self.storage.create_run(
                imported.run_id,
                imported.status,
                imported.started_at,
                imported.config,
                legacy_imported=True,
                incomplete=imported.incomplete,
                summary=imported.summary,
            )
            self.storage.update_run(imported.run_id, ended_at=imported.ended_at, status=imported.status)
            for event in imported.events:
                self.storage.append_event(imported.run_id, event["event_type"], event["created_at"], event["payload"])
            for tick in imported.ticks:
                self.storage.append_tick(imported.run_id, tick["tick_index"], tick["timestamp"], tick)
            for trade in imported.trades:
                self.storage.append_trade(imported.run_id, trade["timestamp"], trade)
        self._broadcast("log_imported", {"imported_runs": len(runs)})
        return len(runs)

    def list_runs(self) -> list[RunSummary]:
        return self.storage.list_runs()

    def get_run(self, run_id: str):
        return self.storage.get_run_detail(run_id)

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.add(subscriber)
            if self._active_run is not None:
                subscriber.put(
                    {
                        "type": "active_run",
                        "payload": {
                            "run_id": self._active_run.run_id,
                            "status": "running",
                            "last_signal": self._active_run.last_signal,
                        },
                    }
                )
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    async def stream(self):
        subscriber = self.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 15)
                    yield f"event: run_event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            self.unsubscribe(subscriber)

    def _create_run(
        self,
        run_id: str,
        started_at: str,
        config: SimulationConfig,
        portfolio: Portfolio,
        strategy: StrategyEngine,
        simulator: Simulator,
        initial_fx_rate: float,
    ) -> None:
        initial_summary = RunSummaryMetrics(starting_capital_twd=config.starting_capital_twd).model_dump()
        self.storage.create_run(run_id, "running", started_at, config.model_dump(), summary=initial_summary)
        self._active_run = ActiveRun(
            run_id=run_id,
            config=config,
            portfolio=portfolio,
            strategy=strategy,
            simulator=simulator,
            last_price=0.0,
            last_fx_rate=initial_fx_rate,
        )

    def _require_run(self, run_id: str) -> RunSummary:
        run = self.storage.get_run(run_id)
        if run is None:
            raise RuntimeError(f"Run not found after creation: {run_id}")
        return run

    def _handle_event(self, run_id: str, event_type: str, payload: dict) -> None:
        created_at = (
            payload.get("timestamp")
            or payload.get("started_at")
            or payload.get("completed_at")
            or datetime.now(timezone.utc).isoformat()
        )
        self.storage.append_event(run_id, event_type, created_at, payload)

        if event_type == "signal_evaluated":
            self.storage.append_tick(run_id, payload["tick_index"], payload["timestamp"], payload)
            self._update_active_state(payload)
            self._update_summary(run_id)
        elif event_type == "tick_failed":
            self.storage.append_tick(run_id, payload["tick_index"], payload["timestamp"], payload)
            self.storage.update_run(run_id, incomplete=True)
        elif event_type == "trade_executed":
            self.storage.append_trade(run_id, payload["timestamp"], payload["trade"])
            self._update_summary(run_id)
        elif event_type == "run_completed":
            self._finish_run(run_id, payload)

        self._broadcast(event_type, payload)

    def _update_active_state(self, payload: dict) -> None:
        with self._lock:
            if self._active_run is None:
                return
            self._active_run.last_price = payload.get("price") or self._active_run.last_price
            self._active_run.last_fx_rate = payload.get("fx_rate") or self._active_run.last_fx_rate
            signal = payload.get("signal")
            if signal:
                self._active_run.last_signal = signal.get("action")

    def _update_summary(self, run_id: str) -> None:
        with self._lock:
            active = self._active_run if self._active_run and self._active_run.run_id == run_id else None
        if active is None:
            detail = self.storage.get_run_detail(run_id)
            if detail is None or not detail.ticks:
                return
            latest_tick = detail.ticks[-1]
            price = latest_tick.price or 0.0
            fx_rate = latest_tick.fx_rate or 1.0
            portfolio_snapshot = latest_tick.portfolio
            summary = RunSummaryMetrics(
                starting_capital_twd=detail.run.config.starting_capital_twd,
                current_value_twd=float(portfolio_snapshot.get("total_value_twd", 0.0)),
                pnl_twd=float(portfolio_snapshot.get("pnl_twd", 0.0)),
                pnl_pct=float(portfolio_snapshot.get("pnl_pct", 0.0)),
                max_drawdown_pct=float(portfolio_snapshot.get("max_drawdown_pct", 0.0)),
                total_fee_twd=sum((trade.fee_twd or (trade.fee_usdt * fx_rate)) for trade in detail.trades),
                win_rate_pct=float(portfolio_snapshot.get("win_rate_pct", 0.0)),
                trade_count=len(detail.trades),
                last_price_twd=(price * fx_rate) if price else None,
                latest_signal=latest_tick.signal["action"] if latest_tick.signal else None,
            )
            self.storage.update_run(run_id, summary=summary.model_dump())
            return

        price = active.last_price
        fx_rate = active.last_fx_rate
        summary = RunSummaryMetrics(
            starting_capital_twd=active.config.starting_capital_twd,
            current_value_twd=active.portfolio.get_total_value(price) * fx_rate if price else 0.0,
            pnl_twd=active.portfolio.get_pnl(price) * fx_rate if price else 0.0,
            pnl_pct=active.portfolio.get_pnl_pct(price) if price else 0.0,
            max_drawdown_pct=active.portfolio.get_max_drawdown(price) if price else 0.0,
            total_fee_twd=sum(trade.fee_usdt for trade in active.portfolio.trade_history) * fx_rate,
            win_rate_pct=active.portfolio.get_win_rate(),
            trade_count=len(active.portfolio.trade_history),
            last_price_twd=price * fx_rate if price else None,
            latest_signal=active.last_signal,
        )
        self.storage.update_run(run_id, summary=summary.model_dump())

    def _finish_run(self, run_id: str, payload: dict) -> None:
        self._update_summary(run_id)
        self.storage.update_run(run_id, status=payload.get("reason", "stopped"), ended_at=payload["completed_at"])
        with self._lock:
            if self._active_run and self._active_run.run_id == run_id:
                self._active_run = None

    def _broadcast(self, event_type: str, payload: dict) -> None:
        event = {"type": event_type, "payload": payload}
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)
