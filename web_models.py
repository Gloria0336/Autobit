from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, root_validator, validator

from config import (
    ANTI_CHASE_PCT,
    CHECK_INTERVAL_SEC,
    DISPLAY_CURRENCY,
    EMA_SIGNAL_PERIOD,
    EMA_TREND_PERIOD,
    EXIT_COOLDOWN_MINUTES,
    FX_RATE_BASE,
    FX_RATE_QUOTE,
    MACD_FAST,
    MACD_SIGNAL_LINE,
    MACD_SLOW,
    RSI_ENTRY_HIGH,
    RSI_ENTRY_LOW,
    RSI_EXIT_HIGH,
    RSI_PERIOD,
    SIGNAL_INTERVAL,
    SOFT_SELL_MIN_PROFIT_PCT,
    STOP_LOSS_PCT,
    SUPPORTED_INTERVALS,
    SYMBOL,
    TRAIL_STOP_PCT,
    TRAIL_TRIGGER_PCT,
    TREND_INTERVAL,
    TRADING_CURRENCY,
)

StrategyParameterName = Literal[
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
]


class SimulationConfig(BaseModel):
    starting_capital_twd: float = Field(10000.0, gt=0)
    check_interval_sec: float = Field(CHECK_INTERVAL_SEC, ge=0.05)
    symbol: str = Field(SYMBOL, min_length=1)
    trend_interval: str = TREND_INTERVAL
    signal_interval: str = SIGNAL_INTERVAL
    rsi_entry_low: float = Field(RSI_ENTRY_LOW, ge=0, le=100)
    rsi_entry_high: float = Field(RSI_ENTRY_HIGH, ge=0, le=100)
    rsi_exit_high: float = Field(RSI_EXIT_HIGH, ge=0, le=100)
    anti_chase_pct: float = Field(ANTI_CHASE_PCT, ge=0, lt=1)
    stop_loss_pct: float = Field(STOP_LOSS_PCT, ge=0, lt=1)
    soft_sell_min_profit_pct: float = Field(SOFT_SELL_MIN_PROFIT_PCT, ge=0, lt=1)
    exit_cooldown_minutes: float = Field(EXIT_COOLDOWN_MINUTES, ge=0)
    trail_trigger_pct: float = Field(TRAIL_TRIGGER_PCT, ge=0, lt=1)
    trail_stop_pct: float = Field(TRAIL_STOP_PCT, ge=0, lt=1)
    ema_trend_period: int = Field(EMA_TREND_PERIOD, ge=2)
    ema_signal_period: int = Field(EMA_SIGNAL_PERIOD, ge=2)
    rsi_period: int = Field(RSI_PERIOD, ge=2)
    macd_fast: int = Field(MACD_FAST, ge=2)
    macd_slow: int = Field(MACD_SLOW, ge=2)
    macd_signal_line: int = Field(MACD_SIGNAL_LINE, ge=2)
    trading_currency: str = TRADING_CURRENCY
    display_currency: str = DISPLAY_CURRENCY
    fx_rate_base: str = FX_RATE_BASE
    fx_rate_quote: str = FX_RATE_QUOTE
    data_source: Literal["live", "historical"] = "live"
    historical_base_interval: str | None = None
    historical_source_mode: Literal["binance_api", "csv_upload"] = "binance_api"
    historical_start_at: datetime | None = None
    historical_end_at: datetime | None = None
    historical_fx_mode: Literal["historical_daily"] = "historical_daily"
    historical_source_filename: str | None = None

    @validator("trend_interval", "signal_interval", "historical_base_interval")
    def validate_interval(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in SUPPORTED_INTERVALS:
            raise ValueError(f"Unsupported interval: {value}")
        return value

    @root_validator(skip_on_failure=True)
    def validate_ranges(cls, values: dict[str, Any]) -> dict[str, Any]:
        if values.get("rsi_entry_low", 0) >= values.get("rsi_entry_high", 0):
            raise ValueError("rsi_entry_low must be less than rsi_entry_high")
        if values.get("rsi_entry_high", 0) >= values.get("rsi_exit_high", 0):
            raise ValueError("rsi_entry_high must be less than rsi_exit_high")
        if values.get("macd_fast", 0) >= values.get("macd_slow", 0):
            raise ValueError("macd_fast must be less than macd_slow")
        if values.get("data_source") == "historical":
            if not values.get("historical_base_interval"):
                raise ValueError("historical_base_interval is required for historical playback")
            if values.get("historical_source_mode") == "binance_api":
                start_at = values.get("historical_start_at")
                end_at = values.get("historical_end_at")
                if start_at is None or end_at is None:
                    raise ValueError(
                        "historical_start_at and historical_end_at are required for Binance historical playback"
                    )
                if end_at <= start_at:
                    raise ValueError("historical_end_at must be later than historical_start_at")
        return values


class StrategyPreset(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    config: SimulationConfig
    created_at: datetime
    updated_at: datetime


class StrategyPresetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    config: SimulationConfig


class RunSummaryMetrics(BaseModel):
    starting_capital_twd: float = 0.0
    current_value_twd: float = 0.0
    pnl_twd: float = 0.0
    pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    total_fee_twd: float = 0.0
    win_rate_pct: float = 0.0
    trade_count: int = 0
    last_price_twd: float | None = None
    latest_signal: str | None = None


class SimulationEvent(BaseModel):
    id: int | None = None
    run_id: str
    event_type: str
    created_at: datetime
    payload: dict[str, Any]


class TradeRecord(BaseModel):
    id: int | None = None
    run_id: str
    timestamp: datetime
    action: Literal["BUY", "SELL"]
    price: float
    btc_amount: float
    gross_usdt: float
    fee_usdt: float
    net_usdt: float
    fee_type: str
    reason: str
    portfolio_value_after: float
    price_twd: float | None = None
    fee_twd: float | None = None
    portfolio_value_after_twd: float | None = None
    market_timestamp: datetime | None = None
    playback_index: int | None = None
    playback_total: int | None = None


class TickSnapshot(BaseModel):
    id: int | None = None
    run_id: str
    tick_index: int
    timestamp: datetime
    status: str
    price: float | None = None
    price_twd: float | None = None
    fx_rate: float | None = None
    fx_date: str | None = None
    indicators: dict[str, Any] = Field(default_factory=dict)
    portfolio: dict[str, Any] = Field(default_factory=dict)
    signal: dict[str, Any] | None = None
    strategy_state: dict[str, Any] | None = None
    next_tick_at: datetime | None = None
    error: str | None = None
    market_timestamp: datetime | None = None
    playback_index: int | None = None
    playback_total: int | None = None


class RunSummary(BaseModel):
    id: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    legacy_imported: bool = False
    incomplete: bool = False
    config: SimulationConfig
    summary: RunSummaryMetrics = Field(default_factory=RunSummaryMetrics)


class RunDetail(BaseModel):
    run: RunSummary
    ticks: list[TickSnapshot]
    trades: list[TradeRecord]
    events: list[SimulationEvent]


class LogImportRequest(BaseModel):
    path: str | None = None


class LogImportResponse(BaseModel):
    imported_runs: int


class StopRunResponse(BaseModel):
    run_id: str
    status: str


class AnalysisObservation(BaseModel):
    title: str
    detail: str
    category: str
    severity: Literal["info", "warning", "critical"] = "info"
    evidence: list[str] = Field(default_factory=list)


class AnalysisReasonStat(BaseModel):
    reason: str
    count: int
    ratio_pct: float


class RoundTripSummary(BaseModel):
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    holding_minutes: float | None = None
    pnl_twd: float = 0.0
    pnl_pct: float = 0.0
    entry_reason: str | None = None
    exit_reason: str | None = None


class DrawdownSegment(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    max_drawdown_pct: float = 0.0
    tick_count: int = 0


class AnalysisTimelineTick(BaseModel):
    tick_index: int
    timestamp: datetime | None = None
    signal: str | None = None
    reason: str | None = None
    price_twd: float | None = None
    rsi: float | None = None
    pnl_pct: float | None = None
    max_drawdown_pct: float | None = None


class AnalysisTimelineTrade(BaseModel):
    timestamp: datetime | None = None
    action: Literal["BUY", "SELL"]
    price_twd: float | None = None
    fee_twd: float | None = None
    btc_amount: float
    reason: str
    portfolio_value_after_twd: float | None = None


class AnalysisRunContext(BaseModel):
    run_id: str
    status: str
    data_source: Literal["live", "historical"]
    symbol: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_sec: float | None = None
    historical_source_filename: str | None = None
    strategy_params: dict[str, Any] = Field(default_factory=dict)


class AnalysisPerformance(BaseModel):
    starting_capital_twd: float = 0.0
    ending_value_twd: float = 0.0
    pnl_twd: float = 0.0
    pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    total_fee_twd: float = 0.0
    win_rate_pct: float = 0.0
    trade_count: int = 0
    tick_count: int = 0
    last_price_twd: float | None = None


class AnalysisTradeBreakdown(BaseModel):
    buy_count: int = 0
    sell_count: int = 0
    round_trip_count: int = 0
    average_round_trip_pnl_twd: float | None = None
    average_winning_round_trip_pnl_twd: float | None = None
    average_losing_round_trip_pnl_twd: float | None = None
    average_holding_minutes: float | None = None
    best_round_trip: RoundTripSummary | None = None
    worst_round_trip: RoundTripSummary | None = None


class AnalysisSignalDiagnostics(BaseModel):
    buy_signal_count: int = 0
    sell_signal_count: int = 0
    hold_signal_count: int = 0
    top_hold_reasons: list[AnalysisReasonStat] = Field(default_factory=list)
    top_sell_reasons: list[AnalysisReasonStat] = Field(default_factory=list)
    latest_signal: str | None = None


class AnalysisRiskDiagnostics(BaseModel):
    stop_loss_trigger_count: int = 0
    trailing_stop_trigger_count: int = 0
    high_drawdown_segments: list[DrawdownSegment] = Field(default_factory=list)
    fee_to_pnl_ratio_pct: float | None = None


class AnalysisTimelineSummary(BaseModel):
    recent_ticks: list[AnalysisTimelineTick] = Field(default_factory=list)
    recent_trades: list[AnalysisTimelineTrade] = Field(default_factory=list)


class StrategyRecommendationContext(BaseModel):
    buy_signal_count: int = 0
    sell_signal_count: int = 0
    hold_signal_count: int = 0
    round_trip_count: int = 0
    win_rate_pct: float = 0.0
    pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    fee_to_pnl_ratio_pct: float | None = None
    stop_loss_trigger_count: int = 0
    trailing_stop_trigger_count: int = 0
    dominant_hold_reason: str | None = None
    dominant_hold_reason_ratio_pct: float = 0.0
    dominant_sell_reason: str | None = None
    dominant_sell_reason_ratio_pct: float = 0.0
    entry_filter_tightness: Literal["low", "medium", "high"] = "medium"
    exit_efficiency: Literal["weak", "mixed", "strong"] = "mixed"


class AnalysisReport(BaseModel):
    generated_at: datetime
    run_context: AnalysisRunContext
    performance: AnalysisPerformance
    trade_breakdown: AnalysisTradeBreakdown
    signal_diagnostics: AnalysisSignalDiagnostics
    risk_diagnostics: AnalysisRiskDiagnostics
    timeline_summary: AnalysisTimelineSummary
    strategy_recommendation_context: StrategyRecommendationContext
    fallback_observations: list[AnalysisObservation] = Field(default_factory=list)


class RunReportResponse(BaseModel):
    report: AnalysisReport
    markdown: str
    prompt: str
    default_model: str | None = None


class AnalysisConfigResponse(BaseModel):
    ai_enabled: bool = False
    default_model: str | None = None
    provider: str = "openrouter"
    recommended_models: list[str] = Field(default_factory=list)


class StrategyRecommendation(BaseModel):
    parameter: StrategyParameterName
    current_value: float | int
    suggested_change: Literal["increase", "decrease", "keep", "test"]
    suggested_value: float | int
    reason: str
    expected_effect: str
    confidence: Literal["low", "medium", "high"]


class AnalyzeReportRequest(BaseModel):
    api_key: str | None = None
    model: str | None = None
    language: str = "zh-TW"
    max_observations: int = Field(5, ge=1, le=12)
    max_recommendations: int = Field(5, ge=1, le=12)


class AnalyzeReportResponse(BaseModel):
    report: AnalysisReport
    markdown: str
    ai_analysis_markdown: str
    recommendations: list[StrategyRecommendation] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    model: str
    generated_at: datetime
    parsing_error: str | None = None


class SaveReportArchiveRequest(BaseModel):
    report: AnalysisReport
    markdown: str
    ai_analysis_markdown: str | None = None
    recommendations: list[StrategyRecommendation] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    model: str | None = None
    generated_at: datetime | None = None
    parsing_error: str | None = None


class SaveReportArchiveResponse(BaseModel):
    run_id: str
    archive_dir: str
    saved_at: datetime
    saved_files: list[str] = Field(default_factory=list)


class AnalysisConnectionTestRequest(BaseModel):
    api_key: str | None = None
    model: str | None = None


class AnalysisConnectionTestResponse(BaseModel):
    ok: bool
    provider: str = "openrouter"
    model: str
    message: str
