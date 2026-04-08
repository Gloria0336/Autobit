from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

import indicators as indicator_module
from config import CHECK_INTERVAL_SEC
from live_market_data import DataFetchError
from market_sources import MarketDataSource, PlaybackCompleted
from web_models import SimulationConfig

if TYPE_CHECKING:
    from display import DisplayManager
    from portfolio import Portfolio
    from strategy import StrategyEngine

log = logging.getLogger("autobit")

EventCallback = Callable[[str, dict[str, Any]], None]


class Simulator:
    def __init__(
        self,
        portfolio: "Portfolio",
        market_source: MarketDataSource,
        strategy: "StrategyEngine",
        display: "DisplayManager | None" = None,
        interval_sec: float = CHECK_INTERVAL_SEC,
        *,
        config: SimulationConfig | None = None,
        event_callback: EventCallback | None = None,
        run_id: str | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.market_source = market_source
        self.strategy = strategy
        self.display = display
        self.interval_sec = interval_sec
        self.config = config or SimulationConfig()
        self.event_callback = event_callback
        self.run_id = run_id

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signal = None
        self._last_price = 0.0
        self._last_indicators: dict[str, float] = {}
        self._last_fx_rate = 1.0
        self._last_fx_date = ""
        self._last_market_timestamp: datetime | None = None
        self._last_playback_index: int | None = None
        self._last_playback_total: int | None = None
        self._tick_index = 0
        self._completion_emitted = False
        self._completion_reason = "stopped"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name=f"simulator-{self.run_id or 'default'}", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "stopped") -> None:
        self._completion_reason = reason
        self._stop_event.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        log.info("Simulation loop started | interval=%.2fs | source=%s", self.interval_sec, self.config.data_source)
        while not self._stop_event.is_set():
            should_continue = self._tick()
            if not should_continue:
                break
            if self._stop_event.wait(self.interval_sec):
                break
        self._emit_run_completed()

    def _tick(self) -> bool:
        if self._stop_event.is_set():
            return False

        self._tick_index += 1
        started_at = datetime.now(timezone.utc)
        next_tick_at = started_at + timedelta(seconds=self.interval_sec)

        self._emit(
            "tick_started",
            {
                "run_id": self.run_id,
                "tick_index": self._tick_index,
                "started_at": started_at.isoformat(),
                "next_tick_at": next_tick_at.isoformat(),
            },
        )

        try:
            snapshot = self.market_source.get_next_snapshot()
        except PlaybackCompleted:
            self._completion_reason = "completed"
            self._stop_event.set()
            return False

        try:
            indicators = indicator_module.compute_all(
                snapshot.trend_df,
                snapshot.signal_df,
                ema_trend_period=self.config.ema_trend_period,
                ema_signal_period=self.config.ema_signal_period,
                rsi_period=self.config.rsi_period,
                macd_fast=self.config.macd_fast,
                macd_slow=self.config.macd_slow,
                macd_signal=self.config.macd_signal_line,
            )

            price = snapshot.price
            fx_rate = snapshot.fx_rate
            fx_date = snapshot.fx_date
            market_timestamp = snapshot.market_timestamp

            portfolio_value = self.portfolio.mark_to_market(price)
            position_cost = self.portfolio.entry_price * self.portfolio.btc_held if self.portfolio.in_position else 0.0

            if self.strategy.in_position:
                self.strategy.update_trailing(price)

            snapshot_payload = {
                "run_id": self.run_id,
                "tick_index": self._tick_index,
                "timestamp": started_at.isoformat(),
                "status": "ok",
                "price": price,
                "price_twd": price * fx_rate,
                "fx_rate": fx_rate,
                "fx_date": fx_date,
                "market_timestamp": market_timestamp.isoformat() if market_timestamp else None,
                "playback_index": snapshot.playback_index,
                "playback_total": snapshot.playback_total,
                "indicators": {
                    **indicators,
                    "ema200_twd": indicators["ema200"] * fx_rate,
                    "ema20_twd": indicators["ema20"] * fx_rate,
                },
                "portfolio": self.portfolio.snapshot(price, fx_rate),
                "strategy_state": self.strategy.snapshot(),
                "next_tick_at": next_tick_at.isoformat(),
            }
            self._emit("market_snapshot", snapshot_payload)

            signal = self.strategy.evaluate(
                price,
                indicators,
                portfolio_value,
                self.portfolio.starting_capital,
                position_cost,
            )
            self._last_signal = signal

            trade_payload: dict[str, Any] | None = None
            trade_kwargs = {
                "market_timestamp": market_timestamp,
                "playback_index": snapshot.playback_index,
                "playback_total": snapshot.playback_total,
            }
            if signal.action == "BUY" and not self.portfolio.in_position:
                trade = self.portfolio.execute_buy(price, signal.fee_type, signal.reason, **trade_kwargs)
                self.strategy.on_entry(price)
                trade_payload = trade.to_dict(fx_rate)
            elif signal.action == "SELL" and self.portfolio.in_position:
                trade = self.portfolio.execute_sell(price, signal.fee_type, signal.reason, **trade_kwargs)
                self.strategy.on_exit()
                trade_payload = trade.to_dict(fx_rate)

            final_snapshot = {
                **snapshot_payload,
                "signal": signal.to_dict(),
                "portfolio": self.portfolio.snapshot(price, fx_rate),
                "strategy_state": self.strategy.snapshot(),
            }
            self._emit("signal_evaluated", final_snapshot)

            if trade_payload is not None:
                self._emit(
                    "trade_executed",
                    {
                        "run_id": self.run_id,
                        "tick_index": self._tick_index,
                        "timestamp": started_at.isoformat(),
                        "trade": trade_payload,
                    },
                )

            self._last_price = price
            self._last_indicators = indicators
            self._last_fx_rate = fx_rate
            self._last_fx_date = fx_date
            self._last_market_timestamp = market_timestamp
            self._last_playback_index = snapshot.playback_index
            self._last_playback_total = snapshot.playback_total

            if self.display is not None:
                self.display.update(price, indicators, self.portfolio, signal, next_tick_at, fx_rate, fx_date)
            return True
        except DataFetchError as exc:
            self._emit_tick_failure(started_at, next_tick_at, str(exc))
            log.warning("Tick failed: %s", exc)
            return True
        except Exception:
            log.exception("Unexpected simulator failure")
            self._emit_tick_failure(started_at, next_tick_at, "Unexpected simulator failure")
            return True

    def _emit_tick_failure(self, started_at: datetime, next_tick_at: datetime, error: str) -> None:
        self._emit(
            "tick_failed",
            {
                "run_id": self.run_id,
                "tick_index": self._tick_index,
                "timestamp": started_at.isoformat(),
                "status": "error",
                "price": self._last_price,
                "fx_rate": self._last_fx_rate,
                "fx_date": self._last_fx_date,
                "market_timestamp": self._last_market_timestamp.isoformat() if self._last_market_timestamp else None,
                "playback_index": self._last_playback_index,
                "playback_total": self._last_playback_total,
                "indicators": self._last_indicators,
                "signal": self._last_signal.to_dict() if self._last_signal else None,
                "error": error,
                "next_tick_at": next_tick_at.isoformat(),
            },
        )

    def _emit_run_completed(self) -> None:
        if self._completion_emitted:
            return
        self._completion_emitted = True
        self._emit(
            "run_completed",
            {
                "run_id": self.run_id,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "reason": self._completion_reason,
                "tick_count": self._tick_index,
            },
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_callback is not None:
            self.event_callback(event_type, payload)
