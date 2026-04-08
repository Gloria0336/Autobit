'''
import logging
from dataclasses import dataclass
from config import (
    RSI_ENTRY_LOW, RSI_ENTRY_HIGH, RSI_EXIT_HIGH,
    ANTI_CHASE_PCT, STOP_LOSS_PCT,
    TRAIL_TRIGGER_PCT, TRAIL_STOP_PCT,
    FEE_TAKER, FEE_MAKER,
)

log = logging.getLogger("autobit")


@dataclass
class SignalResult:
    action: str     # "BUY" | "SELL" | "HOLD"
    reason: str     # 人類可讀的觸發原因
    fee_type: str   # "taker" | "maker" | "none"


class StrategyEngine:
    """
    交易訊號引擎。
    - 持倉狀態由此類管理（與 Portfolio 帳務分離）
    - 每次 Tick 呼叫 evaluate() 取得訊號
    """

    def __init__(self):
        self._in_position: bool        = False
        self._entry_price: float       = 0.0
        self._highest_price: float     = 0.0
        self._trailing_active: bool    = False

    # ── 主要評估介面 ─────────────────────────────────────────────────────────
    def evaluate(
        self,
        current_price: float,
        indicators: dict,
        portfolio_value: float,
        capital_base: float,
        position_cost: float,        # 買入時花費的 TWD（含手續費）
    ) -> SignalResult:
        """
        每次 Tick 呼叫。
        持倉中 → 檢查賣出條件；空倉 → 檢查買入條件。
        """
        if self._in_position:
            return self._check_sell(current_price, indicators, portfolio_value, capital_base, position_cost)
        return self._check_buy(current_price, indicators)

    # ── 買入條件 ─────────────────────────────────────────────────────────────
    def _check_buy(self, price: float, ind: dict) -> SignalResult:
        """
        全部條件皆滿足才回傳 BUY。
        1. 趨勢過濾：price > EMA200
        2. RSI 動能：50 ≤ RSI ≤ 70
        3. MACD 動能：histogram > 0
        4. 防追高：|price - EMA20| / EMA20 ≤ 2%
        """
        # 1. 趨勢過濾器
        if price <= ind["ema200"]:
            return SignalResult("HOLD", f"趨勢偏空：現價 {price:.2f} < EMA200 {ind['ema200']:.2f}", "none")

        # 2. RSI 動能
        rsi = ind["rsi"]
        if not (RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH):
            return SignalResult("HOLD", f"RSI {rsi:.1f} 不在買入區間 [{RSI_ENTRY_LOW}, {RSI_ENTRY_HIGH}]", "none")

        # 3. MACD 動能
        if ind["macd_hist"] <= 0:
            return SignalResult("HOLD", f"MACD 柱狀圖 {ind['macd_hist']:.6f} 非正值", "none")

        # 4. 防追高
        distance_pct = abs(price - ind["ema20"]) / ind["ema20"]
        if distance_pct > ANTI_CHASE_PCT:
            return SignalResult(
                "HOLD",
                f"防追高：現價偏離 EMA20 {distance_pct*100:.2f}%（上限 {ANTI_CHASE_PCT*100:.0f}%）",
                "none"
            )

        return SignalResult(
            "BUY",
            f"順勢+RSI {rsi:.1f}+MACD↑+防追高通過",
            "taker"
        )

    # ── 賣出條件 ─────────────────────────────────────────────────────────────
    def _check_sell(
        self,
        price: float,
        ind: dict,
        portfolio_value: float,
        capital_base: float,
        position_cost: float,
    ) -> SignalResult:
        """
        依優先順序逐一檢查，第一個觸發即回傳 SELL。
        優先順序：硬性停損 > 移動停利 > RSI 耗竭 > MACD 翻轉
        """
        # 1. 硬性停損：虧損 ≥ 總資金的 2%
        if position_cost > 0 and capital_base > 0:
            loss_vs_capital = max(0.0, (capital_base - portfolio_value) / capital_base)
            if loss_vs_capital >= STOP_LOSS_PCT:
                return SignalResult(
                    "SELL",
                    f"硬性停損：總資金回撤 {loss_vs_capital*100:.2f}% 達 {STOP_LOSS_PCT*100:.0f}%",
                    "taker"
                )

        # 2. 移動停利
        if self._trailing_active:
            trail_stop_price = self._highest_price * (1 - TRAIL_STOP_PCT)
            if price <= trail_stop_price:
                return SignalResult(
                    "SELL",
                    f"移動停利觸發：現價 {price:.2f} ≤ 停利價 {trail_stop_price:.2f}（最高 {self._highest_price:.2f}）",
                    "taker"
                )

        # 3. RSI 耗竭
        rsi = ind["rsi"]
        if rsi > RSI_EXIT_HIGH:
            return SignalResult("SELL", f"RSI 耗竭：{rsi:.1f} > {RSI_EXIT_HIGH}", "maker")

        # 4. MACD 柱狀圖由正轉負
        if ind["macd_hist_prev"] > 0 and ind["macd_hist"] < 0:
            return SignalResult(
                "SELL",
                f"MACD 動能翻轉：柱狀圖 {ind['macd_hist_prev']:.6f} → {ind['macd_hist']:.6f}",
                "maker"
            )

        return SignalResult("HOLD", "無賣出條件觸發", "none")

    # ── 狀態更新 ─────────────────────────────────────────────────────────────
    def update_trailing(self, current_price: float) -> None:
        """每次 Tick 持倉時呼叫，更新移動停利追蹤狀態。"""
        if not self._in_position:
            return
        if current_price > self._highest_price:
            self._highest_price = current_price
        unrealized_pct = (current_price - self._entry_price) / self._entry_price
        if unrealized_pct >= TRAIL_TRIGGER_PCT:
            if not self._trailing_active:
                log.info("移動停利啟動：未實現獲利 %.2f%%，最高價 %.2f", unrealized_pct * 100, self._highest_price)
            self._trailing_active = True

    def on_entry(self, price: float) -> None:
        """買入成交後呼叫，設定持倉狀態。"""
        self._in_position     = True
        self._entry_price     = price
        self._highest_price   = price
        self._trailing_active = False
        log.info("進場確認：買入價格 %.2f", price)

    def on_exit(self) -> None:
        """賣出成交後呼叫，清除持倉狀態。"""
        self._in_position     = False
        self._entry_price     = 0.0
        self._highest_price   = 0.0
        self._trailing_active = False
        log.info("出場確認：持倉已清除")

    # ── 查詢屬性 ─────────────────────────────────────────────────────────────
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
'''

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

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

    def evaluate(
        self,
        current_price: float,
        indicators: dict,
        portfolio_value: float,
        capital_base: float,
        position_cost: float,
    ) -> SignalResult:
        if self._in_position:
            return self._check_sell(current_price, indicators, portfolio_value, capital_base, position_cost)
        return self._check_buy(current_price, indicators)

    def _check_buy(self, price: float, indicators: dict) -> SignalResult:
        if price <= indicators["ema200"]:
            return SignalResult(
                "HOLD",
                f"趨勢過濾未通過：price {price:.2f} <= EMA200 {indicators['ema200']:.2f}",
                "none",
            )

        rsi_value = indicators["rsi"]
        if not (self.config.rsi_entry_low <= rsi_value <= self.config.rsi_entry_high):
            return SignalResult(
                "HOLD",
                f"RSI {rsi_value:.1f} 不在買入區間 [{self.config.rsi_entry_low:.1f}, {self.config.rsi_entry_high:.1f}]",
                "none",
            )

        if indicators["macd_hist"] <= 0:
            return SignalResult("HOLD", f"MACD 柱體仍為負值：{indicators['macd_hist']:.6f}", "none")

        distance_pct = abs(price - indicators["ema20"]) / indicators["ema20"]
        if distance_pct > self.config.anti_chase_pct:
            return SignalResult(
                "HOLD",
                f"價格偏離 EMA20 {distance_pct * 100:.2f}% 超過限制 {self.config.anti_chase_pct * 100:.2f}%",
                "none",
            )

        return SignalResult("BUY", f"趨勢、RSI({rsi_value:.1f}) 與 MACD 條件同時成立", "taker")

    def _check_sell(
        self,
        price: float,
        indicators: dict,
        portfolio_value: float,
        capital_base: float,
        position_cost: float,
    ) -> SignalResult:
        if position_cost > 0 and capital_base > 0:
            loss_vs_capital = max(0.0, (capital_base - portfolio_value) / capital_base)
            if loss_vs_capital >= self.config.stop_loss_pct:
                return SignalResult(
                    "SELL",
                    f"硬性停損：總資金回撤 {loss_vs_capital * 100:.2f}% 達 {self.config.stop_loss_pct * 100:.2f}%",
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
            return SignalResult("SELL", f"RSI 過熱：{rsi_value:.1f} > {self.config.rsi_exit_high:.1f}", "maker")

        if indicators["macd_hist_prev"] > 0 and indicators["macd_hist"] < 0:
            return SignalResult(
                "SELL",
                f"MACD 由正轉負：{indicators['macd_hist_prev']:.6f} -> {indicators['macd_hist']:.6f}",
                "maker",
            )

        return SignalResult("HOLD", "持倉中，尚未觸發賣出條件", "none")

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

    def on_exit(self) -> None:
        self._in_position = False
        self._entry_price = 0.0
        self._highest_price = 0.0
        self._trailing_active = False
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

    def snapshot(self) -> dict:
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
            "highest_price": self._highest_price,
            "trailing_active": self._trailing_active,
        }
