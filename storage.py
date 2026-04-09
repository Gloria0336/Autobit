from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from web_models import (
    RunDetail,
    RunSummary,
    RunSummaryMetrics,
    SimulationConfig,
    StrategyPreset,
    SimulationEvent,
    TickSnapshot,
    TradeRecord,
)


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    legacy_imported INTEGER NOT NULL DEFAULT 0,
                    incomplete INTEGER NOT NULL DEFAULT 0,
                    config_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tick_index INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS historical_fx_rates (
                    requested_date TEXT PRIMARY KEY,
                    resolved_date TEXT NOT NULL,
                    rate REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_presets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "UPDATE runs SET status = 'stopped', ended_at = COALESCE(ended_at, started_at) WHERE status = 'running'"
            )

    def create_run(
        self,
        run_id: str,
        status: str,
        started_at: str,
        config: dict,
        *,
        legacy_imported: bool = False,
        incomplete: bool = False,
        summary: dict | None = None,
    ) -> None:
        summary_payload = summary or RunSummaryMetrics().model_dump()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (id, status, started_at, ended_at, legacy_imported, incomplete, config_json, summary_json)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    started_at,
                    int(legacy_imported),
                    int(incomplete),
                    json.dumps(config, ensure_ascii=False),
                    json.dumps(summary_payload, ensure_ascii=False),
                ),
            )

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        ended_at: str | None = None,
        summary: dict | None = None,
        incomplete: bool | None = None,
    ) -> None:
        assignments: list[str] = []
        params: list[object] = []
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if ended_at is not None:
            assignments.append("ended_at = ?")
            params.append(ended_at)
        if summary is not None:
            assignments.append("summary_json = ?")
            params.append(json.dumps(summary, ensure_ascii=False))
        if incomplete is not None:
            assignments.append("incomplete = ?")
            params.append(int(incomplete))
        if not assignments:
            return
        params.append(run_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?", params)

    def append_event(self, run_id: str, event_type: str, created_at: str, payload: dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO events (run_id, event_type, created_at, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, event_type, created_at, json.dumps(payload, ensure_ascii=False)),
            )

    def append_tick(self, run_id: str, tick_index: int, created_at: str, payload: dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO ticks (run_id, tick_index, created_at, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, tick_index, created_at, json.dumps(payload, ensure_ascii=False)),
            )

    def append_trade(self, run_id: str, created_at: str, payload: dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO trades (run_id, created_at, payload_json) VALUES (?, ?, ?)",
                (run_id, created_at, json.dumps(payload, ensure_ascii=False)),
            )

    def get_historical_fx_rate(self, requested_date: str) -> tuple[float, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rate, resolved_date FROM historical_fx_rates WHERE requested_date = ?",
                (requested_date,),
            ).fetchone()
        if row is None:
            return None
        return float(row["rate"]), str(row["resolved_date"])

    def cache_historical_fx_rate(self, requested_date: str, resolved_date: str, rate: float) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO historical_fx_rates (requested_date, resolved_date, rate)
                VALUES (?, ?, ?)
                ON CONFLICT(requested_date) DO UPDATE SET
                    resolved_date = excluded.resolved_date,
                    rate = excluded.rate
                """,
                (requested_date, resolved_date, rate),
            )

    def list_strategy_presets(self) -> list[StrategyPreset]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM strategy_presets ORDER BY updated_at DESC, created_at DESC").fetchall()
        return [self._row_to_strategy_preset(row) for row in rows]

    def save_strategy_preset(
        self,
        preset_id: str,
        name: str,
        config: dict,
        *,
        created_at: str,
        updated_at: str,
    ) -> StrategyPreset:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_presets (id, name, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (
                    preset_id,
                    name,
                    json.dumps(config, ensure_ascii=False),
                    created_at,
                    updated_at,
                ),
            )
            row = connection.execute("SELECT * FROM strategy_presets WHERE id = ?", (preset_id,)).fetchone()
        if row is None:
            raise KeyError(preset_id)
        return self._row_to_strategy_preset(row)

    def delete_strategy_preset(self, preset_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM strategy_presets WHERE id = ?", (preset_id,))
        return cursor.rowcount > 0

    def list_runs(self) -> list[RunSummary]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
        return [self._row_to_run_summary(row) for row in rows]

    def get_run(self, run_id: str) -> RunSummary | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_run_summary(row)

    def get_run_detail(self, run_id: str) -> RunDetail | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        with self._connect() as connection:
            tick_rows = connection.execute(
                "SELECT * FROM ticks WHERE run_id = ? ORDER BY tick_index ASC, id ASC",
                (run_id,),
            ).fetchall()
            trade_rows = connection.execute(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY created_at ASC, id ASC",
                (run_id,),
            ).fetchall()
            event_rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        ticks = [self._row_to_tick(row) for row in tick_rows]
        trades = [self._row_to_trade(run_id, row) for row in trade_rows]
        events = [self._row_to_event(run_id, row) for row in event_rows]
        return RunDetail(run=run, ticks=ticks, trades=trades, events=events)

    def _row_to_run_summary(self, row: sqlite3.Row) -> RunSummary:
        return RunSummary(
            id=row["id"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            legacy_imported=bool(row["legacy_imported"]),
            incomplete=bool(row["incomplete"]),
            config=SimulationConfig.model_validate(json.loads(row["config_json"])),
            summary=RunSummaryMetrics.model_validate(json.loads(row["summary_json"])),
        )

    def _row_to_strategy_preset(self, row: sqlite3.Row) -> StrategyPreset:
        return StrategyPreset(
            id=row["id"],
            name=row["name"],
            config=SimulationConfig.model_validate(json.loads(row["config_json"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_tick(self, row: sqlite3.Row) -> TickSnapshot:
        payload = json.loads(row["payload_json"])
        payload["id"] = row["id"]
        payload["run_id"] = row["run_id"]
        return TickSnapshot.model_validate(payload)

    def _row_to_trade(self, run_id: str, row: sqlite3.Row) -> TradeRecord:
        payload = json.loads(row["payload_json"])
        payload["id"] = row["id"]
        payload["run_id"] = run_id
        payload["timestamp"] = payload.get("timestamp", row["created_at"])
        return TradeRecord.model_validate(payload)

    def _row_to_event(self, run_id: str, row: sqlite3.Row) -> SimulationEvent:
        return SimulationEvent(
            id=row["id"],
            run_id=run_id,
            event_type=row["event_type"],
            created_at=row["created_at"],
            payload=json.loads(row["payload_json"]),
        )
