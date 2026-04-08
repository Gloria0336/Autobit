'''
import logging
from dataclasses import dataclass, field
from datetime import datetime
from config import FEE_TAKER, FEE_MAKER

log = logging.getLogger("autobit")


@dataclass
class Trade:
    timestamp: datetime
    action: str           # "BUY" | "SELL"
    price: float
    btc_amount: float
    gross_usdt: float     # 交易前的 TWD 金額（含手續費）
    fee_usdt: float
    net_usdt: float       # 實際入帳 / 實際花費（扣除手續費後）
    fee_type: str         # "taker" | "maker"
    reason: str
    portfolio_value_after: float


class Portfolio:
    def __init__(self, starting_capital: float):
        self.starting_capital: float = starting_capital
        self.cash: float             = starting_capital   # TWD 現金
        self.btc_held: float         = 0.0                # 持有 BTC 數量
        self.trade_history: list[Trade] = []
        self._peak_value: float      = starting_capital
        self._entry_price: float     = 0.0

    # ── 執行買入 ──────────────────────────────────────────────────────────────
    def execute_buy(self, price: float, fee_type: str, reason: str) -> Trade:
        """
        用全部現金買入 BTC（Taker 市價單）。
        btc_bought = cash * (1 - fee_rate) / price
        """
        fee_rate   = FEE_TAKER if fee_type == "taker" else FEE_MAKER
        gross      = self.cash
        fee_usdt   = gross * fee_rate
        net_spend  = gross - fee_usdt
        btc_bought = net_spend / price

        self._entry_price = price
        self.btc_held     = btc_bought
        self.cash         = 0.0

        pv = self.get_total_value(price)
        self._update_peak(pv)

        trade = Trade(
            timestamp=datetime.now(),
            action="BUY",
            price=price,
            btc_amount=btc_bought,
            gross_usdt=gross,
            fee_usdt=fee_usdt,
            net_usdt=net_spend,
            fee_type=fee_type,
            reason=reason,
            portfolio_value_after=pv,
        )
        self.trade_history.append(trade)
        log.info(
            "BUY  | 價格 %.2f | BTC %.6f | 手續費 %.4f TWD (%s) | %s",
            price, btc_bought, fee_usdt, fee_type, reason
        )
        return trade

    # ── 執行賣出 ──────────────────────────────────────────────────────────────
    def execute_sell(self, price: float, fee_type: str, reason: str) -> Trade:
        """
        賣出全部 BTC。
        net_usdt = btc_held * price * (1 - fee_rate)
        """
        fee_rate   = FEE_TAKER if fee_type == "taker" else FEE_MAKER
        gross_usdt = self.btc_held * price
        fee_usdt   = gross_usdt * fee_rate
        net_usdt   = gross_usdt - fee_usdt
        btc_sold   = self.btc_held

        self.cash     = net_usdt
        self.btc_held = 0.0
        self._entry_price = 0.0

        pv = self.get_total_value(price)
        self._update_peak(pv)

        trade = Trade(
            timestamp=datetime.now(),
            action="SELL",
            price=price,
            btc_amount=btc_sold,
            gross_usdt=gross_usdt,
            fee_usdt=fee_usdt,
            net_usdt=net_usdt,
            fee_type=fee_type,
            reason=reason,
            portfolio_value_after=pv,
        )
        self.trade_history.append(trade)
        log.info(
            "SELL | 價格 %.2f | BTC %.6f | 手續費 %.4f TWD (%s) | %s",
            price, btc_sold, fee_usdt, fee_type, reason
        )
        return trade

    # ── 查詢函數 ──────────────────────────────────────────────────────────────
    def get_total_value(self, current_price: float) -> float:
        """總資金 = 現金 + BTC 市值。"""
        return self.cash + self.btc_held * current_price

    def mark_to_market(self, current_price: float) -> float:
        """用當前市價更新淨值高點，並回傳最新總資金。"""
        total_value = self.get_total_value(current_price)
        self._update_peak(total_value)
        return total_value

    def get_pnl(self, current_price: float) -> float:
        return self.get_total_value(current_price) - self.starting_capital

    def get_pnl_pct(self, current_price: float) -> float:
        return self.get_pnl(current_price) / self.starting_capital * 100

    def get_unrealized_pnl(self, current_price: float) -> float:
        """未實現損益（僅持倉時有效）。"""
        if self.btc_held == 0 or self._entry_price == 0:
            return 0.0
        return (current_price - self._entry_price) * self.btc_held

    def get_unrealized_pnl_pct(self, current_price: float) -> float:
        if self.btc_held == 0 or self._entry_price == 0:
            return 0.0
        return (current_price - self._entry_price) / self._entry_price * 100

    def get_max_drawdown(self, current_price: float) -> float:
        """最大回撤百分比（正值表示回撤幅度）。"""
        current = self.get_total_value(current_price)
        if self._peak_value == 0:
            return 0.0
        return max(0.0, (self._peak_value - current) / self._peak_value * 100)

    def get_win_rate(self) -> float:
        """勝率：獲利 SELL 筆數 / 全部 SELL 筆數 * 100。"""
        sells = [t for t in self.trade_history if t.action == "SELL"]
        if not sells:
            return 0.0
        buys = [t for t in self.trade_history if t.action == "BUY"]
        buy_map = {}
        buy_idx = 0
        wins = 0
        for sell in sells:
            if buy_idx < len(buys):
                if sell.net_usdt > buys[buy_idx].gross_usdt:
                    wins += 1
                buy_idx += 1
        return wins / len(sells) * 100

    @property
    def entry_price(self) -> float:
        return self._entry_price

    @property
    def in_position(self) -> bool:
        return self.btc_held > 0

    def _update_peak(self, value: float) -> None:
        if value > self._peak_value:
            self._peak_value = value
'''

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from config import FEE_MAKER, FEE_TAKER

log = logging.getLogger("autobit")


@dataclass
class Trade:
    timestamp: datetime
    action: str
    price: float
    btc_amount: float
    gross_usdt: float
    fee_usdt: float
    net_usdt: float
    fee_type: str
    reason: str
    portfolio_value_after: float
    market_timestamp: datetime | None = None
    playback_index: int | None = None
    playback_total: int | None = None

    def to_dict(self, fx_rate: float | None = None) -> dict:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        if self.market_timestamp is not None:
            payload["market_timestamp"] = self.market_timestamp.isoformat()
        if fx_rate is not None:
            payload["price_twd"] = self.price * fx_rate
            payload["fee_twd"] = self.fee_usdt * fx_rate
            payload["portfolio_value_after_twd"] = self.portfolio_value_after * fx_rate
        return payload


class Portfolio:
    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.btc_held = 0.0
        self.trade_history: list[Trade] = []
        self._peak_value = starting_capital
        self._entry_price = 0.0

    def execute_buy(
        self,
        price: float,
        fee_type: str,
        reason: str,
        *,
        market_timestamp: datetime | None = None,
        playback_index: int | None = None,
        playback_total: int | None = None,
    ) -> Trade:
        fee_rate = FEE_TAKER if fee_type == "taker" else FEE_MAKER
        gross = self.cash
        fee_usdt = gross * fee_rate
        net_spend = gross - fee_usdt
        btc_bought = net_spend / price

        self._entry_price = price
        self.btc_held = btc_bought
        self.cash = 0.0

        portfolio_value = self.get_total_value(price)
        self._update_peak(portfolio_value)

        trade = Trade(
            timestamp=datetime.now(timezone.utc),
            action="BUY",
            price=price,
            btc_amount=btc_bought,
            gross_usdt=gross,
            fee_usdt=fee_usdt,
            net_usdt=net_spend,
            fee_type=fee_type,
            reason=reason,
            portfolio_value_after=portfolio_value,
            market_timestamp=market_timestamp,
            playback_index=playback_index,
            playback_total=playback_total,
        )
        self.trade_history.append(trade)
        log.info(
            "BUY | price=%.2f | btc=%.6f | fee=%.4f USDT (%s) | %s",
            price,
            btc_bought,
            fee_usdt,
            fee_type,
            reason,
        )
        return trade

    def execute_sell(
        self,
        price: float,
        fee_type: str,
        reason: str,
        *,
        market_timestamp: datetime | None = None,
        playback_index: int | None = None,
        playback_total: int | None = None,
    ) -> Trade:
        fee_rate = FEE_TAKER if fee_type == "taker" else FEE_MAKER
        gross_usdt = self.btc_held * price
        fee_usdt = gross_usdt * fee_rate
        net_usdt = gross_usdt - fee_usdt
        btc_sold = self.btc_held

        self.cash = net_usdt
        self.btc_held = 0.0
        self._entry_price = 0.0

        portfolio_value = self.get_total_value(price)
        self._update_peak(portfolio_value)

        trade = Trade(
            timestamp=datetime.now(timezone.utc),
            action="SELL",
            price=price,
            btc_amount=btc_sold,
            gross_usdt=gross_usdt,
            fee_usdt=fee_usdt,
            net_usdt=net_usdt,
            fee_type=fee_type,
            reason=reason,
            portfolio_value_after=portfolio_value,
            market_timestamp=market_timestamp,
            playback_index=playback_index,
            playback_total=playback_total,
        )
        self.trade_history.append(trade)
        log.info(
            "SELL | price=%.2f | btc=%.6f | fee=%.4f USDT (%s) | %s",
            price,
            btc_sold,
            fee_usdt,
            fee_type,
            reason,
        )
        return trade

    def get_total_value(self, current_price: float) -> float:
        return self.cash + self.btc_held * current_price

    def mark_to_market(self, current_price: float) -> float:
        total_value = self.get_total_value(current_price)
        self._update_peak(total_value)
        return total_value

    def get_pnl(self, current_price: float) -> float:
        return self.get_total_value(current_price) - self.starting_capital

    def get_pnl_pct(self, current_price: float) -> float:
        return self.get_pnl(current_price) / self.starting_capital * 100

    def get_unrealized_pnl(self, current_price: float) -> float:
        if self.btc_held == 0 or self._entry_price == 0:
            return 0.0
        return (current_price - self._entry_price) * self.btc_held

    def get_unrealized_pnl_pct(self, current_price: float) -> float:
        if self.btc_held == 0 or self._entry_price == 0:
            return 0.0
        return (current_price - self._entry_price) / self._entry_price * 100

    def get_max_drawdown(self, current_price: float) -> float:
        current_value = self.get_total_value(current_price)
        if self._peak_value == 0:
            return 0.0
        return max(0.0, (self._peak_value - current_value) / self._peak_value * 100)

    def get_win_rate(self) -> float:
        sells = [trade for trade in self.trade_history if trade.action == "SELL"]
        if not sells:
            return 0.0
        buys = [trade for trade in self.trade_history if trade.action == "BUY"]
        wins = 0
        for buy, sell in zip(buys, sells):
            if sell.net_usdt > buy.gross_usdt:
                wins += 1
        return wins / len(sells) * 100

    def snapshot(self, current_price: float, fx_rate: float | None = None) -> dict:
        total_value = self.get_total_value(current_price)
        snapshot = {
            "cash_usdt": self.cash,
            "btc_held": self.btc_held,
            "entry_price": self._entry_price,
            "in_position": self.in_position,
            "total_value_usdt": total_value,
            "pnl_usdt": self.get_pnl(current_price),
            "pnl_pct": self.get_pnl_pct(current_price),
            "unrealized_pnl_usdt": self.get_unrealized_pnl(current_price),
            "unrealized_pnl_pct": self.get_unrealized_pnl_pct(current_price),
            "max_drawdown_pct": self.get_max_drawdown(current_price),
            "win_rate_pct": self.get_win_rate(),
            "trade_count": len(self.trade_history),
        }
        if fx_rate is not None:
            snapshot["cash_twd"] = self.cash * fx_rate
            snapshot["entry_price_twd"] = self._entry_price * fx_rate if self._entry_price else 0.0
            snapshot["total_value_twd"] = total_value * fx_rate
            snapshot["pnl_twd"] = snapshot["pnl_usdt"] * fx_rate
            snapshot["unrealized_pnl_twd"] = snapshot["unrealized_pnl_usdt"] * fx_rate
        return snapshot

    @property
    def entry_price(self) -> float:
        return self._entry_price

    @property
    def in_position(self) -> bool:
        return self.btc_held > 0

    def _update_peak(self, value: float) -> None:
        if value > self._peak_value:
            self._peak_value = value
