from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from web_models import SimulationConfig

log = logging.getLogger("autobit")


@dataclass
class SignalResult:
    action: str
    reason: str
    fee_type: str

    def to_dict(self) -> dict:
        return asdict(self)


class StrategyEngine:
    def __init__(self, config: SimulationConfig | None = None):
        self.config = config or SimulationConfig()
        self._in_position = False
        self._entry_price = 0.0
        self._highest_price = 0.0
        self._trailing_active = False
        self._last_exit_at: datetime | None = None

    def evaluate(
        self,
        current_price: float,
        indicators: dict,
        portfolio_value: float,
        capital_base: float,
        position_cost: float,
        signal_time: datetime | None = None,
    ) -> SignalResult:
        if self._in_position:
            return self._check_sell(current_price, indicators, portfolio_value, capital_base, position_cost)
        return self._check_buy(current_price, indicators, signal_time=signal_time)

    def _check_buy(self, price: float, indicators: dict, *, signal_time: datetime | None = None) -> SignalResult:
        cooldown_remaining_minutes = self._get_cooldown_remaining_minutes(signal_time)
        if cooldown_remaining_minutes > 0:
            return SignalResult(
                "HOLD",
                f"Exit cooldown active: {cooldown_remaining_minutes:.2f} min remaining",
                "none",
            )

        if price <= indicators["ema200"]:
            return SignalResult("HOLD", f"Trend filter failed: price {price:.2f} <= EMA200 {indicators['ema200']:.2f}", "none")

        rsi_value = indicators["rsi"]
        if not (self.config.rsi_entry_low <= rsi_value <= self.config.rsi_entry_high):
            return SignalResult(
                "HOLD",
                f"RSI {rsi_value:.1f} not in buy range [{self.config.rsi_entry_low:.1f}, {self.config.rsi_entry_high:.1f}]",
                "none",
            )

        if indicators["macd_hist"] <= 0:
            return SignalResult("HOLD", f"MACD histogram not positive: {indicators['macd_hist']:.6f}", "none")

        distance_pct = abs(price - indicators["ema20"]) / indicators["ema20"]
        if distance_pct > self.config.anti_chase_pct:
            return SignalResult(
                "HOLD",
                f"Anti-chase filter: distance from EMA20 {distance_pct * 100:.2f}% exceeds {self.config.anti_chase_pct * 100:.2f}%",
                "none",
            )

        return SignalResult("BUY", f"Trend, RSI({rsi_value:.1f}), and MACD conditions aligned", "taker")

    def _check_sell(
        self,
        price: float,
        indicators: dict,
        portfolio_value: float,
        capital_base: float,
        position_cost: float,
    ) -> SignalResult:
        del capital_base
        if position_cost > 0:
            loss_vs_entry_cost = max(0.0, (position_cost - portfolio_value) / position_cost)
            if loss_vs_entry_cost >= self.config.stop_loss_pct:
                return SignalResult(
                    "SELL",
                    (
                        f"Hard stop-loss: drawdown vs entry cost {loss_vs_entry_cost * 100:.2f}% "
                        f">= {self.config.stop_loss_pct * 100:.2f}%"
                    ),
                    "taker",
                )

        if self._trailing_active:
            trailing_stop_price = self._highest_price * (1 - self.config.trail_stop_pct)
            if price <= trailing_stop_price:
                return SignalResult(
                    "SELL",
                    f"移動停利觸發：price {price:.2f} <= stop {trailing_stop_price:.2f}，高點 {self._highest_price:.2f}",
                    "taker",
                )

        rsi_value = indicators["rsi"]
        if rsi_value > self.config.rsi_exit_high:
            soft_sell_guard = self._check_soft_sell_profit_threshold(price)
            if soft_sell_guard is not None:
                return soft_sell_guard
            return SignalResult("SELL", f"RSI 過熱：{rsi_value:.1f} > {self.config.rsi_exit_high:.1f}", "maker")

        if indicators["macd_hist_prev"] > 0 and indicators["macd_hist"] < 0:
            soft_sell_guard = self._check_soft_sell_profit_threshold(price)
            if soft_sell_guard is not None:
                return soft_sell_guard
            return SignalResult(
                "SELL",
                f"MACD bearish reversal: {indicators['macd_hist_prev']:.6f} -> {indicators['macd_hist']:.6f}",
                "maker",
            )

        return SignalResult("HOLD", "No sell condition matched", "none")

    def update_trailing(self, current_price: float) -> None:
        if not self._in_position:
            return
        if current_price > self._highest_price:
            self._highest_price = current_price
        unrealized_pct = (current_price - self._entry_price) / self._entry_price
        if unrealized_pct >= self.config.trail_trigger_pct:
            if not self._trailing_active:
                log.info(
                    "Trailing stop activated | unrealized_pct=%.4f | highest_price=%.2f",
                    unrealized_pct,
                    self._highest_price,
                )
            self._trailing_active = True

    def on_entry(self, price: float) -> None:
        self._in_position = True
        self._entry_price = price
        self._highest_price = price
        self._trailing_active = False
        log.info("Entered position | entry_price=%.2f", price)

    def on_exit(self, signal_time: datetime | None = None) -> None:
        self._in_position = False
        self._entry_price = 0.0
        self._highest_price = 0.0
        self._trailing_active = False
        self._last_exit_at = self._resolve_signal_time(signal_time)
        log.info("Exited position")

    @property
    def in_position(self) -> bool:
        return self._in_position

    @property
    def trailing_active(self) -> bool:
        return self._trailing_active

    @property
    def highest_price(self) -> float:
        return self._highest_price

    @property
    def entry_price(self) -> float:
        return self._entry_price

    def snapshot(self, signal_time: datetime | None = None) -> dict:
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
            "highest_price": self._highest_price,
            "trailing_active": self._trailing_active,
            "last_exit_at": self._last_exit_at.isoformat() if self._last_exit_at else None,
            "cooldown_remaining_minutes": self._get_cooldown_remaining_minutes(signal_time),
        }

    def _check_soft_sell_profit_threshold(self, price: float) -> SignalResult | None:
        if self.config.soft_sell_min_profit_pct <= 0 or self._entry_price <= 0:
            return None
        unrealized_profit_pct = (price - self._entry_price) / self._entry_price
        if unrealized_profit_pct >= self.config.soft_sell_min_profit_pct:
            return None
        return SignalResult(
            "HOLD",
            (
                f"Soft sell deferred: profit {unrealized_profit_pct * 100:.2f}% "
                f"below min {self.config.soft_sell_min_profit_pct * 100:.2f}%"
            ),
            "none",
        )

    def _get_cooldown_remaining_minutes(self, signal_time: datetime | None) -> float:
        if self._in_position or self._last_exit_at is None or self.config.exit_cooldown_minutes <= 0:
            return 0.0
        now = self._resolve_signal_time(signal_time)
        elapsed_minutes = max(0.0, (now - self._last_exit_at).total_seconds() / 60)
        return max(0.0, self.config.exit_cooldown_minutes - elapsed_minutes)

    def _resolve_signal_time(self, signal_time: datetime | None) -> datetime:
        if signal_time is None:
            return datetime.now(timezone.utc)
        if signal_time.tzinfo is None:
            return signal_time.replace(tzinfo=timezone.utc)
        return signal_time
