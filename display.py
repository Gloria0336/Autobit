'''
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.prompt import FloatPrompt
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from portfolio import Portfolio
    from strategy import SignalResult

CLR_UP = "bright_green"
CLR_DOWN = "bright_red"
CLR_NEUT = "bright_white"
CLR_DIM = "grey70"
CLR_BUY = "bright_green"
CLR_SELL = "bright_red"
CLR_HOLD = "grey70"


class DisplayManager:
    def __init__(self, console: Console):
        self.console = console
        self.layout = self._build_layout()
        self._prev_price = 0.0
        self._price = 0.0
        self._ind: dict = {}
        self._portfolio = None
        self._last_signal = None
        self._next_tick: datetime | None = None
        self._fx_rate = 1.0
        self._fx_date = ""
        self._start_time = datetime.now()
        self._render_layout()

    def set_fx_context(self, fx_rate: float, fx_date: str) -> None:
        self._fx_rate = fx_rate
        self._fx_date = fx_date
        self._render_layout()

    def update(
        self,
        price: float,
        indicators: dict,
        portfolio: "Portfolio",
        last_signal: "SignalResult",
        next_tick_at: datetime,
        fx_rate: float,
        fx_date: str,
    ) -> None:
        self._prev_price = self._price or price
        self._price = price
        self._ind = indicators
        self._portfolio = portfolio
        self._last_signal = last_signal
        self._next_tick = next_tick_at
        self._fx_rate = fx_rate
        self._fx_date = fx_date
        self._render_layout()

    def _build_layout(self) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="top_row", size=8),
            Layout(name="portfolio", size=7),
            Layout(name="signal", size=5),
            Layout(name="trades", size=14),
            Layout(name="footer", size=3),
        )
        layout["top_row"].split_row(Layout(name="price", ratio=1), Layout(name="indicators", ratio=2))
        return layout

    def _render_layout(self) -> None:
        self.layout["header"].update(self._make_header())
        self.layout["price"].update(self._make_price_panel())
        self.layout["indicators"].update(self._make_indicator_panel())
        self.layout["portfolio"].update(self._make_portfolio_panel())
        self.layout["signal"].update(self._make_signal_panel())
        self.layout["trades"].update(self._make_trade_table())
        self.layout["footer"].update(self._make_footer())

    def _to_twd(self, amount: float) -> float:
        return amount * self._fx_rate

    def _make_header(self) -> Panel:
        elapsed = datetime.now() - self._start_time
        h, rem = divmod(int(elapsed.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        title = Text("AUTOBIT - 比特幣交易模擬器", style="bold bright_yellow", justify="center")
        fx_text = f"1 USDT ~= {self._fx_rate:,.2f} TWD ({self._fx_date})" if self._fx_date else "等待匯率資料..."
        sub = Text(
            f"  運行時間 {h:02d}:{m:02d}:{s:02d}  ·  BTC/USDT 行情 / TWD 顯示  ·  {fx_text}  ·  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            style=CLR_DIM,
            justify="center",
        )
        return Panel(Text.assemble(title, "\n", sub), style="bold", box=box.DOUBLE_EDGE)

    def _make_price_panel(self) -> Panel:
        price_twd = self._to_twd(self._price)
        prev_twd = self._to_twd(self._prev_price)
        change = price_twd - prev_twd
        pct = (change / prev_twd * 100) if prev_twd else 0.0
        color = CLR_UP if change >= 0 else CLR_DOWN
        arrow = "▲" if change >= 0 else "▼"
        txt = Text()
        txt.append(f"NT${price_twd:,.2f}\n", style=f"bold {color}")
        txt.append(f"{arrow} {change:+.2f}  ({pct:+.3f}%)", style=color)
        return Panel(txt, title="[bold]即時價格（TWD）[/bold]", border_style=color, padding=(1, 2))

    def _make_indicator_panel(self) -> Panel:
        if not self._ind:
            return Panel("[dim]等待資料...[/dim]", title="[bold]技術指標[/bold]")

        price_twd = self._to_twd(self._price)
        ema200 = self._to_twd(self._ind.get("ema200", 0.0))
        ema20 = self._to_twd(self._ind.get("ema20", 0.0))
        rsi_val = self._ind.get("rsi", 50.0)
        mhist = self._to_twd(self._ind.get("macd_hist", 0.0))
        mhist_prev = self._to_twd(self._ind.get("macd_hist_prev", 0.0))
        trend_txt, trend_clr = ("多頭趨勢", CLR_UP) if price_twd > ema200 else ("空頭趨勢", CLR_DOWN)
        rsi_clr = CLR_DOWN if rsi_val > 70 else (CLR_UP if rsi_val > 50 else CLR_DIM)
        macd_clr = CLR_UP if mhist > 0 else CLR_DOWN
        macd_arr = "↑" if mhist > mhist_prev else "↓"
        dist = abs(price_twd - ema20) / ema20 * 100 if ema20 else 0.0
        dist_clr = CLR_DOWN if dist > 2 else CLR_UP

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(style="bold " + CLR_DIM, width=18)
        table.add_column()
        table.add_row("趨勢 (EMA200)", Text(f"{trend_txt}  EMA200=NT${ema200:,.2f}", style=trend_clr))
        table.add_row("EMA20", Text(f"NT${ema20:,.2f}  偏離 {dist:.2f}%", style=dist_clr))
        table.add_row("RSI(14)", Text(f"{rsi_val:.2f}", style=rsi_clr))
        table.add_row("MACD Hist", Text(f"{mhist:.4f} {macd_arr}", style=macd_clr))
        table.add_row("MACD Prev", Text(f"{mhist_prev:.4f}", style=CLR_DIM))
        return Panel(table, title="[bold]技術指標（TWD 顯示）[/bold]", border_style=CLR_NEUT)

    def _make_portfolio_panel(self) -> Panel:
        if self._portfolio is None:
            return Panel("[dim]等待資料...[/dim]", title="[bold]帳務總覽[/bold]")

        portfolio = self._portfolio
        price = self._price
        total_value = self._to_twd(portfolio.get_total_value(price))
        pnl = self._to_twd(portfolio.get_pnl(price))
        pnl_pct = portfolio.get_pnl_pct(price)
        unrealized = self._to_twd(portfolio.get_unrealized_pnl(price))
        unrealized_pct = portfolio.get_unrealized_pnl_pct(price)
        drawdown = portfolio.get_max_drawdown(price)
        win_rate = portfolio.get_win_rate()
        pnl_color = CLR_UP if pnl >= 0 else CLR_DOWN
        unrealized_color = CLR_UP if unrealized >= 0 else CLR_DOWN

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column(style="bold " + CLR_DIM, width=16)
        table.add_column()
        table.add_column(style="bold " + CLR_DIM, width=16)
        table.add_column()
        table.add_row(
            "起始本金",
            Text(f"NT${self._to_twd(portfolio.starting_capital):,.2f}", style=CLR_NEUT),
            "總資金",
            Text(f"NT${total_value:,.2f}", style=CLR_NEUT),
        )
        table.add_row(
            "現金",
            Text(f"NT${self._to_twd(portfolio.cash):,.2f}", style=CLR_NEUT),
            "持有 BTC",
            Text(f"{portfolio.btc_held:.6f} BTC", style=CLR_NEUT),
        )
        table.add_row(
            "總損益",
            Text(f"NT${pnl:+,.2f} ({pnl_pct:+.2f}%)", style=pnl_color),
            "未實現損益",
            Text(f"NT${unrealized:+,.2f} ({unrealized_pct:+.2f}%)", style=unrealized_color),
        )
        table.add_row(
            "最大回撤",
            Text(f"{drawdown:.2f}%", style=CLR_DOWN if drawdown > 0 else CLR_DIM),
            "交易勝率",
            Text(f"{win_rate:.1f}%", style=CLR_UP if win_rate >= 50 else CLR_DOWN),
        )
        return Panel(table, title="[bold]帳務總覽（TWD）[/bold]", border_style="bright_cyan")

    def _make_signal_panel(self) -> Panel:
        if self._last_signal is None:
            return Panel("[dim]等待第一次 Tick...[/dim]", title="[bold]最新訊號[/bold]")

        sig = self._last_signal
        color = {"BUY": CLR_BUY, "SELL": CLR_SELL, "HOLD": CLR_HOLD}.get(sig.action, CLR_HOLD)
        fee = {"maker": "Maker 0.08%", "taker": "Taker 0.16%", "none": "-"}.get(sig.fee_type, "-")
        txt = Text()
        txt.append(f"  {sig.action}  ", style=f"bold reverse {color}")
        txt.append(f"  手續費：{fee}\n", style=CLR_DIM)
        txt.append(f"  {sig.reason}", style=CLR_NEUT)
        return Panel(txt, title="[bold]最新訊號[/bold]", border_style=color)

    def _make_trade_table(self) -> Panel:
        if self._portfolio is None:
            return Panel("[dim]尚無交易紀錄[/dim]", title="[bold]交易紀錄（最近 10 筆）[/bold]")

        history = self._portfolio.trade_history[-10:][::-1]
        table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold " + CLR_DIM, padding=(0, 1))
        table.add_column("時間", width=19)
        table.add_column("動作", width=6, justify="center")
        table.add_column("價格", width=14, justify="right")
        table.add_column("BTC 數量", width=12, justify="right")
        table.add_column("手續費", width=12, justify="right")
        table.add_column("費率", width=8, justify="center")
        table.add_column("原因", ratio=1)

        if not history:
            table.add_row("[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]尚無交易[/dim]")
        else:
            for trade in history:
                color = CLR_BUY if trade.action == "BUY" else CLR_SELL
                table.add_row(
                    trade.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    Text(trade.action, style=f"bold {color}"),
                    f"NT${self._to_twd(trade.price):,.2f}",
                    f"{trade.btc_amount:.6f}",
                    f"NT${self._to_twd(trade.fee_usdt):,.2f}",
                    trade.fee_type,
                    Text(trade.reason[:50], style=CLR_DIM),
                )
        return Panel(table, title="[bold]交易紀錄（最近 10 筆）[/bold]", border_style=CLR_DIM)

    def _make_footer(self) -> Panel:
        if self._next_tick is None:
            return Panel(Text("正在初始化...", justify="center", style=CLR_DIM), box=box.SIMPLE)
        remaining = max(0, (self._next_tick - datetime.now()).total_seconds())
        m, s = divmod(int(remaining), 60)
        txt = Text(f"下次檢查倒數：{m:02d}:{s:02d}  ·  按 Ctrl+C 退出", justify="center", style=CLR_DIM)
        return Panel(txt, box=box.SIMPLE)

    def prompt_starting_capital(self) -> float:
        self.console.print()
        self.console.rule("[bold bright_yellow]AUTOBIT 比特幣交易模擬器[/bold bright_yellow]")
        self.console.print()
        while True:
            capital = FloatPrompt.ask("[bold]請輸入起始本金（TWD，例如 10000）[/bold]", console=self.console)
            if capital > 0:
                return float(capital)
'''

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.prompt import FloatPrompt
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from portfolio import Portfolio
    from strategy import SignalResult


class DisplayManager:
    def __init__(self, console: Console):
        self.console = console
        self.layout = self._build_layout()
        self._price = 0.0
        self._prev_price = 0.0
        self._ind: dict = {}
        self._portfolio: Portfolio | None = None
        self._last_signal: SignalResult | None = None
        self._next_tick: datetime | None = None
        self._fx_rate = 1.0
        self._fx_date = ""
        self._started_at = datetime.now()
        self._render_layout()

    def set_fx_context(self, fx_rate: float, fx_date: str) -> None:
        self._fx_rate = fx_rate
        self._fx_date = fx_date
        self._render_layout()

    def update(
        self,
        price: float,
        indicators: dict,
        portfolio: "Portfolio",
        last_signal: "SignalResult",
        next_tick_at: datetime,
        fx_rate: float,
        fx_date: str,
    ) -> None:
        self._prev_price = self._price or price
        self._price = price
        self._ind = indicators
        self._portfolio = portfolio
        self._last_signal = last_signal
        self._next_tick = next_tick_at
        self._fx_rate = fx_rate
        self._fx_date = fx_date
        self._render_layout()

    def _build_layout(self) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="top", size=8),
            Layout(name="middle", size=8),
            Layout(name="trades", size=12),
            Layout(name="footer", size=3),
        )
        layout["top"].split_row(Layout(name="price"), Layout(name="signal"))
        layout["middle"].split_row(Layout(name="indicators"), Layout(name="portfolio"))
        return layout

    def _render_layout(self) -> None:
        self.layout["header"].update(self._make_header())
        self.layout["price"].update(self._make_price_panel())
        self.layout["signal"].update(self._make_signal_panel())
        self.layout["indicators"].update(self._make_indicator_panel())
        self.layout["portfolio"].update(self._make_portfolio_panel())
        self.layout["trades"].update(self._make_trade_panel())
        self.layout["footer"].update(self._make_footer())

    def _to_twd(self, amount: float) -> float:
        return amount * self._fx_rate

    def _make_header(self) -> Panel:
        elapsed = datetime.now() - self._started_at
        return Panel(
            Text(
                f"AUTOBIT CLI  |  已執行 {elapsed}  |  1 USDT ≈ {self._fx_rate:,.2f} TWD ({self._fx_date or 'n/a'})",
                justify="center",
            ),
            box=box.DOUBLE,
        )

    def _make_price_panel(self) -> Panel:
        change = self._price - self._prev_price
        change_color = "green" if change >= 0 else "red"
        text = Text()
        text.append(f"USDT {self._price:,.2f}\n", style=f"bold {change_color}")
        text.append(f"TWD  {self._to_twd(self._price):,.2f}", style="bold cyan")
        return Panel(text, title="即時價格", border_style=change_color)

    def _make_signal_panel(self) -> Panel:
        if self._last_signal is None:
            return Panel("等待第一個 tick...", title="最後訊號")
        color = {"BUY": "green", "SELL": "red", "HOLD": "yellow"}.get(self._last_signal.action, "white")
        text = Text()
        text.append(f"{self._last_signal.action}\n", style=f"bold {color}")
        text.append(self._last_signal.reason)
        return Panel(text, title="最後訊號", border_style=color)

    def _make_indicator_panel(self) -> Panel:
        if not self._ind:
            return Panel("尚無指標資料", title="指標")
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("key", style="cyan", width=16)
        table.add_column("value")
        table.add_row("EMA200", f"{self._ind.get('ema200', 0.0):,.2f}")
        table.add_row("EMA20", f"{self._ind.get('ema20', 0.0):,.2f}")
        table.add_row("RSI", f"{self._ind.get('rsi', 0.0):,.2f}")
        table.add_row("MACD Hist", f"{self._ind.get('macd_hist', 0.0):,.6f}")
        return Panel(table, title="技術指標")

    def _make_portfolio_panel(self) -> Panel:
        if self._portfolio is None:
            return Panel("尚未建立投資組合", title="資產")
        snapshot = self._portfolio.snapshot(self._price, self._fx_rate)
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("key", style="cyan", width=16)
        table.add_column("value")
        table.add_row("現金", f"USDT {self._portfolio.cash:,.2f}")
        table.add_row("BTC 持有", f"{self._portfolio.btc_held:.6f}")
        table.add_row("總價值", f"TWD {snapshot.get('total_value_twd', 0.0):,.2f}")
        table.add_row("PnL", f"TWD {snapshot.get('pnl_twd', 0.0):+,.2f}")
        table.add_row("勝率", f"{snapshot.get('win_rate_pct', 0.0):.1f}%")
        return Panel(table, title="投資組合")

    def _make_trade_panel(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True)
        table.add_column("時間", width=20)
        table.add_column("動作", width=6)
        table.add_column("價格", justify="right", width=12)
        table.add_column("BTC", justify="right", width=12)
        table.add_column("理由")
        if not self._portfolio or not self._portfolio.trade_history:
            table.add_row("-", "-", "-", "-", "尚無交易")
        else:
            for trade in self._portfolio.trade_history[-8:][::-1]:
                table.add_row(
                    trade.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    trade.action,
                    f"{trade.price:,.2f}",
                    f"{trade.btc_amount:.6f}",
                    trade.reason[:60],
                )
        return Panel(table, title="最近交易")

    def _make_footer(self) -> Panel:
        if self._next_tick is None:
            message = "等待下一個 tick..."
        else:
            remaining = max(0, int((self._next_tick - datetime.now()).total_seconds()))
            minutes, seconds = divmod(remaining, 60)
            message = f"下次檢查倒數 {minutes:02d}:{seconds:02d}  |  Ctrl+C 結束"
        return Panel(Text(message, justify="center"), box=box.SIMPLE)

    def prompt_starting_capital(self) -> float:
        while True:
            capital = FloatPrompt.ask("請輸入起始本金（TWD）", console=self.console)
            if capital > 0:
                return float(capital)
            self.console.print("[red]起始本金必須大於 0[/red]")
            '''
            capital = FloatPrompt.ask("請輸入起始本金（TWD）", console=self.console)
            if capital > 0:
                return float(capital)
            self.console.print("[red]本金必須大於 0，請重新輸入。[/red]")
            '''
