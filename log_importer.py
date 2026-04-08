from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from web_models import RunSummaryMetrics, SimulationConfig

TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>[A-Z]+)\] (?P<msg>.*)$")
INDICATOR_RE = re.compile(
    r"EMA200=(?P<ema200>-?\d+(?:\.\d+)?)\s+EMA20=(?P<ema20>-?\d+(?:\.\d+)?)\s+RSI=(?P<rsi>-?\d+(?:\.\d+)?)\s+MACD_H=(?P<macd>-?\d+(?:\.\d+)?)"
)
SIGNAL_RE = re.compile(r"\|\s*(?P<action>BUY|SELL|HOLD)\s*\|\s*(?P<reason>.+)$")
TRADE_RE = re.compile(
    r"(?P<action>BUY|SELL)\s+\|\s+price=(?P<price>-?\d+(?:\.\d+)?)\s+\|\s+btc=(?P<btc>-?\d+(?:\.\d+)?)\s+\|\s+fee=(?P<fee>-?\d+(?:\.\d+)?)\s+USDT\s+\((?P<fee_type>\w+)\)\s+\|\s+(?P<reason>.+)$"
)


@dataclass
class ImportedRun:
    run_id: str
    started_at: str
    status: str = "completed"
    ended_at: str | None = None
    incomplete: bool = True
    config: dict[str, Any] = field(default_factory=lambda: SimulationConfig().model_dump())
    summary: dict[str, Any] = field(default_factory=lambda: RunSummaryMetrics().model_dump())
    ticks: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class LegacyLogImporter:
    def parse(self, path: str | Path) -> list[ImportedRun]:
        log_path = Path(path)
        if not log_path.exists():
            raise FileNotFoundError(log_path)

        runs: list[ImportedRun] = []
        current_run: ImportedRun | None = None
        current_tick: dict[str, Any] | None = None

        for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = TIMESTAMP_RE.match(raw_line.strip())
            if not match:
                continue
            timestamp = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S").isoformat()
            message = match.group("msg")

            if current_run is None:
                current_run = ImportedRun(run_id=f"legacy-{uuid4().hex[:12]}", started_at=timestamp)
                runs.append(current_run)

            current_run.events.append(
                {"run_id": current_run.run_id, "event_type": "legacy_log", "created_at": timestamp, "payload": {"line": message}}
            )

            if "Simulation loop started" in message or ("Tick" in message and not current_run.ticks and not current_run.trades):
                if current_run.ticks or current_run.trades or current_run.ended_at:
                    current_run = ImportedRun(run_id=f"legacy-{uuid4().hex[:12]}", started_at=timestamp)
                    runs.append(current_run)
                current_run.started_at = timestamp

            if "Tick started" in message:
                tick_index = len(current_run.ticks) + 1
                current_tick = {
                    "run_id": current_run.run_id,
                    "tick_index": tick_index,
                    "timestamp": timestamp,
                    "status": "ok",
                    "indicators": {},
                    "portfolio": {},
                }
                current_run.ticks.append(current_tick)
                continue

            indicator_match = INDICATOR_RE.search(message)
            if indicator_match:
                if current_tick is None:
                    current_tick = {
                        "run_id": current_run.run_id,
                        "tick_index": len(current_run.ticks) + 1,
                        "timestamp": timestamp,
                        "status": "ok",
                        "indicators": {},
                        "portfolio": {},
                    }
                    current_run.ticks.append(current_tick)
                current_tick["indicators"] = {
                    "ema200": float(indicator_match.group("ema200")),
                    "ema20": float(indicator_match.group("ema20")),
                    "rsi": float(indicator_match.group("rsi")),
                    "macd_hist": float(indicator_match.group("macd")),
                }
                continue

            signal_match = SIGNAL_RE.search(message)
            if signal_match and current_tick is not None:
                current_tick["signal"] = {
                    "action": signal_match.group("action"),
                    "reason": signal_match.group("reason"),
                    "fee_type": "none",
                }
                current_run.summary["latest_signal"] = signal_match.group("action")
                continue

            trade_match = TRADE_RE.search(message)
            if trade_match:
                current_run.trades.append(
                    {
                        "run_id": current_run.run_id,
                        "timestamp": timestamp,
                        "action": trade_match.group("action"),
                        "price": float(trade_match.group("price")),
                        "btc_amount": float(trade_match.group("btc")),
                        "gross_usdt": 0.0,
                        "fee_usdt": float(trade_match.group("fee")),
                        "net_usdt": 0.0,
                        "fee_type": trade_match.group("fee_type"),
                        "reason": trade_match.group("reason"),
                        "portfolio_value_after": 0.0,
                    }
                )
                current_run.summary["trade_count"] = len(current_run.trades)
                continue

            if "Simulation stopped" in message or "Simulation completed" in message:
                current_run.status = "stopped"
                current_run.ended_at = timestamp

        for run in runs:
            if run.ended_at is None:
                run.ended_at = run.ticks[-1]["timestamp"] if run.ticks else run.started_at
            run.summary["trade_count"] = len(run.trades)
        return runs
