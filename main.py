'''
import logging
import logging.handlers
import sys

from rich.console import Console
from rich.live import Live

from config import CHECK_INTERVAL_SEC, LOG_FILE
from display import DisplayManager
from live_market_data import DataFetchError, MarketDataFetcher
from portfolio import Portfolio
from simulator import Simulator
from strategy import StrategyEngine


def setup_logger() -> None:
    logger = logging.getLogger("autobit")
    logger.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(file_handler)
    logger.propagate = False


def print_summary(portfolio: Portfolio, console: Console, fx_rate: float) -> None:
    console.print()
    console.rule("[bold bright_yellow]模擬結束摘要[/bold bright_yellow]")

    trades = portfolio.trade_history
    sells = [t for t in trades if t.action == "SELL"]
    buys = [t for t in trades if t.action == "BUY"]
    total_fee_twd = sum(t.fee_usdt for t in trades) * fx_rate

    console.print(f"  起始本金：[bold]NT${portfolio.starting_capital * fx_rate:,.2f}[/bold] TWD")
    console.print(f"  交易次數：買入 {len(buys)} 次 / 賣出 {len(sells)} 次")
    console.print(f"  總手續費：[red]NT${total_fee_twd:,.4f}[/red] TWD")
    console.print(f"  交易勝率：[green]{portfolio.get_win_rate():.1f}%[/green]")
    console.print(f"  日誌檔案：{LOG_FILE}")
    console.print()


def main() -> None:
    setup_logger()
    log = logging.getLogger("autobit")

    console = Console()
    display = DisplayManager(console)
    fetcher = MarketDataFetcher()

    try:
        starting_capital_twd = display.prompt_starting_capital()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]已取消。[/dim]")
        sys.exit(0)

    try:
        startup_fx_rate, startup_fx_date = fetcher.get_display_fx_rate()
    except DataFetchError as e:
        console.print(f"[red]啟動失敗：{e}[/red]")
        sys.exit(1)

    display.set_fx_context(startup_fx_rate, startup_fx_date)
    starting_capital_usdt = starting_capital_twd / startup_fx_rate
    log.info(
        "模擬器啟動 | 起始本金 %.2f TWD ≈ %.2f USDT | 匯率日期 %s",
        starting_capital_twd,
        starting_capital_usdt,
        startup_fx_date,
    )

    portfolio = Portfolio(starting_capital_usdt)
    strategy = StrategyEngine()
    simulator = Simulator(portfolio, fetcher, strategy, display, CHECK_INTERVAL_SEC)

    console.print(
        f"\n[dim]正在連線至幣安 API，每 {CHECK_INTERVAL_SEC // 60} 分鐘檢查一次；行情採 BTC/USDT，畫面金額換算為 TWD。[/dim]\n"
    )

    try:
        with Live(display.layout, console=console, refresh_per_second=1, screen=True):
            simulator.start()
            simulator._stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        simulator.stop()
        console.clear()
        print_summary(portfolio, console, display._fx_rate or startup_fx_rate)
        log.info("模擬器正常退出。")


if __name__ == "__main__":
    main()
'''

from __future__ import annotations

import argparse
import logging
import logging.handlers

import uvicorn

from config import LOG_FILE
from web_app import create_app


def setup_logger() -> None:
    logger = logging.getLogger("autobit")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(file_handler)
    logger.propagate = False


setup_logger()
app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autobit Web 控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=False)
