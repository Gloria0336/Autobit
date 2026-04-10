from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from analysis_ai import ReportAnalysisService
from analysis_report import RunReportService
from config import (
    AI_REPORTS_DIR,
    LOG_FILE,
    OPENROUTER_MODEL_CANDIDATES,
    WEB_DATABASE_PATH,
    WEB_DIR,
    WEB_INDEX_FILE,
    WEB_TITLE,
    get_openrouter_settings,
)
from historical_data import HistoricalDataError
from openrouter_client import OpenRouterClient, OpenRouterConfigurationError, OpenRouterRequestError
from report_archive import ReportArchiveService
from run_manager import RunManager
from storage import Storage
from web_models import (
    AnalysisConfigResponse,
    AnalysisConnectionTestRequest,
    AnalysisConnectionTestResponse,
    AnalyzeReportRequest,
    AnalyzeReportResponse,
    LogImportRequest,
    LogImportResponse,
    RunDetail,
    RunReportResponse,
    SaveReportArchiveRequest,
    SaveReportArchiveResponse,
    RunSummary,
    SimulationConfig,
    StopRunResponse,
    StrategyPreset,
    StrategyPresetCreateRequest,
)


def create_app(
    *,
    db_path: str | Path = WEB_DATABASE_PATH,
    run_manager: RunManager | None = None,
) -> FastAPI:
    storage = Storage(db_path)
    manager = run_manager or RunManager(storage, log_path=LOG_FILE)

    app = FastAPI(title=WEB_TITLE)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.storage = storage
    app.state.run_manager = manager
    app.state.report_service = RunReportService()
    app.state.openrouter_client = OpenRouterClient()
    app.state.report_archive_service = ReportArchiveService(AI_REPORTS_DIR)
    app.state.report_analysis_service = ReportAnalysisService(
        report_service=app.state.report_service,
        openrouter_client=app.state.openrouter_client,
        default_model=get_openrouter_settings()["model"],
    )
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return WEB_INDEX_FILE.read_text(encoding="utf-8")

    @app.get("/api/config/defaults", response_model=SimulationConfig)
    def config_defaults() -> SimulationConfig:
        return SimulationConfig()

    @app.get("/api/strategy-presets", response_model=list[StrategyPreset])
    def list_strategy_presets() -> list[StrategyPreset]:
        return app.state.storage.list_strategy_presets()

    @app.post("/api/strategy-presets", response_model=StrategyPreset)
    def create_strategy_preset(payload: StrategyPresetCreateRequest) -> StrategyPreset:
        now = _utc_now_iso()
        return app.state.storage.save_strategy_preset(
            uuid4().hex,
            payload.name.strip(),
            payload.config.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    @app.put("/api/strategy-presets/{preset_id}", response_model=StrategyPreset)
    def update_strategy_preset(preset_id: str, payload: StrategyPresetCreateRequest) -> StrategyPreset:
        existing = next((item for item in app.state.storage.list_strategy_presets() if item.id == preset_id), None)
        if existing is None:
            raise HTTPException(status_code=404, detail="Strategy preset not found")
        return app.state.storage.save_strategy_preset(
            preset_id,
            payload.name.strip(),
            payload.config.model_dump(mode="json"),
            created_at=existing.created_at.isoformat(),
            updated_at=_utc_now_iso(),
        )

    @app.delete("/api/strategy-presets/{preset_id}")
    def delete_strategy_preset(preset_id: str) -> dict[str, str]:
        if not app.state.storage.delete_strategy_preset(preset_id):
            raise HTTPException(status_code=404, detail="Strategy preset not found")
        return {"status": "deleted", "id": preset_id}

    @app.get("/api/config/analysis", response_model=AnalysisConfigResponse)
    def analysis_defaults() -> AnalysisConfigResponse:
        settings = get_openrouter_settings()
        return AnalysisConfigResponse(
            ai_enabled=bool(settings["api_key"] and settings["model"]),
            default_model=settings["model"] or None,
            recommended_models=list(OPENROUTER_MODEL_CANDIDATES),
        )

    @app.post("/api/analysis/test", response_model=AnalysisConnectionTestResponse)
    def test_analysis_connection(payload: AnalysisConnectionTestRequest, request: Request) -> AnalysisConnectionTestResponse:
        settings = get_openrouter_settings()
        model = (payload.model or settings["model"]).strip()
        try:
            result = app.state.openrouter_client.test_connection(
                model=model,
                api_key=payload.api_key,
                referer=request.headers.get("referer"),
                title=request.headers.get("x-title"),
            )
        except OpenRouterConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OpenRouterRequestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return AnalysisConnectionTestResponse(
            ok=True,
            provider="openrouter",
            model=result.model,
            message=result.content,
        )

    @app.get("/api/runs", response_model=list[RunSummary])
    def list_runs() -> list[RunSummary]:
        return app.state.run_manager.list_runs()

    @app.get("/api/runs/{run_id}", response_model=RunDetail)
    def run_detail(run_id: str) -> RunDetail:
        detail = app.state.run_manager.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return detail

    @app.get("/api/runs/{run_id}/report", response_model=RunReportResponse)
    def run_report(run_id: str) -> RunReportResponse:
        detail = app.state.run_manager.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return app.state.report_service.build_response(detail, default_model=get_openrouter_settings()["model"])

    @app.get("/api/runs/{run_id}/report.md", response_class=PlainTextResponse)
    def run_report_markdown(run_id: str) -> str:
        detail = app.state.run_manager.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return app.state.report_service.build_response(detail, default_model=get_openrouter_settings()["model"]).markdown

    @app.post("/api/runs/{run_id}/report/analyze", response_model=AnalyzeReportResponse)
    def analyze_run_report(run_id: str, payload: AnalyzeReportRequest, request: Request) -> AnalyzeReportResponse:
        detail = app.state.run_manager.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            response = app.state.report_analysis_service.analyze_run(
                detail,
                payload,
                referer=request.headers.get("referer"),
                title=request.headers.get("x-title"),
            )
            app.state.report_archive_service.save_analysis_response(run_id, response)
            return response
        except OpenRouterConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except OpenRouterRequestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/report/archive", response_model=SaveReportArchiveResponse)
    def archive_run_report(run_id: str, payload: SaveReportArchiveRequest) -> SaveReportArchiveResponse:
        detail = app.state.run_manager.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return app.state.report_archive_service.save_run_report(run_id, payload)

    @app.post("/api/runs", response_model=RunSummary)
    def create_run(config: SimulationConfig) -> RunSummary:
        try:
            return app.state.run_manager.start_run(config)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/historical", response_model=RunSummary)
    async def create_historical_run(request: Request) -> RunSummary:
        fields, upload = await _parse_multipart_form(request)
        payload = _coerce_form_config(fields)
        payload["data_source"] = "historical"
        if upload is not None:
            payload["historical_source_mode"] = "csv_upload"
            payload["historical_source_filename"] = upload["filename"] or "historical.csv"
        else:
            payload.setdefault("historical_source_mode", "binance_api")
        try:
            config = SimulationConfig.model_validate(payload)
            if upload is None:
                config = config.model_copy(
                    update={
                        "historical_source_filename": config.historical_source_filename or _build_binance_source_label(config),
                    }
                )
            return app.state.run_manager.start_historical_run(
                config,
                upload["content"] if upload is not None else None,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        except HistoricalDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/stop", response_model=StopRunResponse)
    def stop_run(run_id: str) -> StopRunResponse:
        try:
            run = app.state.run_manager.stop_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return StopRunResponse(run_id=run.id, status=run.status)

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(app.state.run_manager.stream(), media_type="text/event-stream")

    @app.post("/api/import/log", response_model=LogImportResponse)
    def import_log(payload: LogImportRequest) -> LogImportResponse:
        try:
            count = app.state.run_manager.import_log(payload.path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return LogImportResponse(imported_runs=count)

    return app


def _coerce_form_config(form) -> dict:
    numeric_fields = {
        "starting_capital_twd",
        "check_interval_sec",
        "rsi_entry_low",
        "rsi_entry_high",
        "rsi_exit_high",
        "anti_chase_pct",
        "stop_loss_pct",
        "soft_sell_min_profit_pct",
        "exit_cooldown_minutes",
        "trail_trigger_pct",
        "trail_stop_pct",
        "ema_trend_period",
        "ema_signal_period",
        "rsi_period",
        "macd_fast",
        "macd_slow",
        "macd_signal_line",
    }
    payload: dict[str, object] = {}
    for key in form.keys():
        value = form.get(key)
        if value in (None, ""):
            continue
        payload[key] = float(value) if key in numeric_fields else value
        if key in {"ema_trend_period", "ema_signal_period", "rsi_period", "macd_fast", "macd_slow", "macd_signal_line"}:
            payload[key] = int(float(value))
    payload.setdefault("data_source", "historical")
    payload.setdefault("historical_source_mode", "binance_api")
    return payload


def _build_binance_source_label(config: SimulationConfig) -> str:
    start_at = config.historical_start_at.isoformat() if config.historical_start_at else "unknown-start"
    end_at = config.historical_end_at.isoformat() if config.historical_end_at else "unknown-end"
    base_interval = config.historical_base_interval or "unknown-interval"
    return f"binance:{config.symbol}:{base_interval}:{start_at}->{end_at}"


async def _parse_multipart_form(request: Request) -> tuple[dict[str, str], dict[str, object] | None]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="Expected multipart/form-data")

    body = await request.body()
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    fields: dict[str, str] = {}
    upload: dict[str, object] | None = None

    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        if filename is None:
            fields[name] = content.decode(part.get_content_charset() or "utf-8")
        else:
            upload = {
                "filename": filename,
                "content": content,
                "content_type": part.get_content_type(),
            }

    return fields, upload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
