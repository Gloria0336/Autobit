from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from web_models import (
    AnalysisObservation,
    AnalysisPerformance,
    AnalysisReasonStat,
    AnalysisReport,
    AnalysisRiskDiagnostics,
    AnalysisRunContext,
    AnalysisSignalDiagnostics,
    AnalysisTimelineSummary,
    AnalysisTimelineTick,
    AnalysisTimelineTrade,
    AnalysisTradeBreakdown,
    DrawdownSegment,
    RoundTripSummary,
    RunDetail,
    RunReportResponse,
    StrategyRecommendationContext,
)

STRATEGY_PARAM_KEYS = (
    "trend_interval",
    "signal_interval",
    "rsi_entry_low",
    "rsi_entry_high",
    "rsi_exit_high",
    "anti_chase_pct",
    "stop_loss_pct",
    "trail_trigger_pct",
    "trail_stop_pct",
    "ema_trend_period",
    "ema_signal_period",
    "rsi_period",
    "macd_fast",
    "macd_slow",
    "macd_signal_line",
)

ENTRY_FILTER_HINTS = ("ema200", "trend", "rsi")
STOP_LOSS_HINTS = ("stop", "loss", "停損", "stop_loss")
TRAILING_HINTS = ("trail", "trailing", "移動停利")


class RunReportService:
    def build_response(self, detail: RunDetail, *, default_model: str | None = None) -> RunReportResponse:
        report = self.build_report(detail)
        markdown = self.render_markdown(report)
        prompt = self.build_prompt(report, markdown)
        return RunReportResponse(report=report, markdown=markdown, prompt=prompt, default_model=default_model or None)

    def build_report(self, detail: RunDetail) -> AnalysisReport:
        now = datetime.now(timezone.utc)
        run = detail.run
        ticks = detail.ticks
        trades = detail.trades

        round_trips = self._pair_round_trips(trades)
        top_hold_reasons = self._reason_stats(
            [tick.signal.get("reason", "") for tick in ticks if tick.signal and tick.signal.get("action") == "HOLD"]
        )
        top_sell_reasons = self._reason_stats(
            [tick.signal.get("reason", "") for tick in ticks if tick.signal and tick.signal.get("action") == "SELL"]
        )

        latest_tick = ticks[-1] if ticks else None
        ended_at = run.ended_at or self._get_tick_time(latest_tick)
        duration_sec = None
        if ended_at is not None:
            duration_sec = max(0.0, (ended_at - run.started_at).total_seconds())

        performance = AnalysisPerformance(
            starting_capital_twd=run.summary.starting_capital_twd,
            ending_value_twd=run.summary.current_value_twd,
            pnl_twd=run.summary.pnl_twd,
            pnl_pct=run.summary.pnl_pct,
            max_drawdown_pct=run.summary.max_drawdown_pct,
            total_fee_twd=run.summary.total_fee_twd,
            win_rate_pct=run.summary.win_rate_pct,
            trade_count=run.summary.trade_count,
            tick_count=len(ticks),
            last_price_twd=run.summary.last_price_twd,
        )

        trade_breakdown = self._build_trade_breakdown(round_trips, trades)
        signal_diagnostics = AnalysisSignalDiagnostics(
            buy_signal_count=sum(1 for tick in ticks if tick.signal and tick.signal.get("action") == "BUY"),
            sell_signal_count=sum(1 for tick in ticks if tick.signal and tick.signal.get("action") == "SELL"),
            hold_signal_count=sum(1 for tick in ticks if tick.signal and tick.signal.get("action") == "HOLD"),
            top_hold_reasons=top_hold_reasons,
            top_sell_reasons=top_sell_reasons,
            latest_signal=(latest_tick.signal or {}).get("action") if latest_tick and latest_tick.signal else run.summary.latest_signal,
        )

        fee_to_pnl_ratio_pct = None
        if performance.pnl_twd != 0:
            fee_to_pnl_ratio_pct = abs(performance.total_fee_twd / performance.pnl_twd) * 100

        risk_diagnostics = AnalysisRiskDiagnostics(
            stop_loss_trigger_count=sum(1 for trade in trades if trade.action == "SELL" and self._matches_any(trade.reason, STOP_LOSS_HINTS)),
            trailing_stop_trigger_count=sum(
                1 for trade in trades if trade.action == "SELL" and self._matches_any(trade.reason, TRAILING_HINTS)
            ),
            high_drawdown_segments=self._build_drawdown_segments(ticks),
            fee_to_pnl_ratio_pct=fee_to_pnl_ratio_pct,
        )

        recommendation_context = StrategyRecommendationContext(
            buy_signal_count=signal_diagnostics.buy_signal_count,
            sell_signal_count=signal_diagnostics.sell_signal_count,
            hold_signal_count=signal_diagnostics.hold_signal_count,
            round_trip_count=trade_breakdown.round_trip_count,
            win_rate_pct=performance.win_rate_pct,
            pnl_pct=performance.pnl_pct,
            max_drawdown_pct=performance.max_drawdown_pct,
            fee_to_pnl_ratio_pct=fee_to_pnl_ratio_pct,
            stop_loss_trigger_count=risk_diagnostics.stop_loss_trigger_count,
            trailing_stop_trigger_count=risk_diagnostics.trailing_stop_trigger_count,
            dominant_hold_reason=top_hold_reasons[0].reason if top_hold_reasons else None,
            dominant_hold_reason_ratio_pct=top_hold_reasons[0].ratio_pct if top_hold_reasons else 0.0,
            dominant_sell_reason=top_sell_reasons[0].reason if top_sell_reasons else None,
            dominant_sell_reason_ratio_pct=top_sell_reasons[0].ratio_pct if top_sell_reasons else 0.0,
            entry_filter_tightness=self._estimate_entry_filter_tightness(signal_diagnostics, performance),
            exit_efficiency=self._estimate_exit_efficiency(performance, risk_diagnostics),
        )

        report = AnalysisReport(
            generated_at=now,
            run_context=AnalysisRunContext(
                run_id=run.id,
                status=run.status,
                data_source=run.config.data_source,
                symbol=run.config.symbol,
                started_at=run.started_at,
                ended_at=ended_at,
                duration_sec=duration_sec,
                historical_source_filename=run.config.historical_source_filename,
                strategy_params={key: getattr(run.config, key, None) for key in STRATEGY_PARAM_KEYS},
            ),
            performance=performance,
            trade_breakdown=trade_breakdown,
            signal_diagnostics=signal_diagnostics,
            risk_diagnostics=risk_diagnostics,
            timeline_summary=AnalysisTimelineSummary(
                recent_ticks=[
                    AnalysisTimelineTick(
                        tick_index=tick.tick_index,
                        timestamp=self._get_tick_time(tick),
                        signal=(tick.signal or {}).get("action"),
                        reason=(tick.signal or {}).get("reason") or tick.error,
                        price_twd=tick.price_twd,
                        rsi=self._safe_float(tick.indicators.get("rsi")) if tick.indicators else None,
                        pnl_pct=self._safe_float(tick.portfolio.get("pnl_pct")) if tick.portfolio else None,
                        max_drawdown_pct=self._safe_float(tick.portfolio.get("max_drawdown_pct")) if tick.portfolio else None,
                    )
                    for tick in ticks[-10:]
                ],
                recent_trades=[
                    AnalysisTimelineTrade(
                        timestamp=trade.market_timestamp or trade.timestamp,
                        action=trade.action,
                        price_twd=trade.price_twd,
                        fee_twd=trade.fee_twd,
                        btc_amount=trade.btc_amount,
                        reason=trade.reason,
                        portfolio_value_after_twd=trade.portfolio_value_after_twd,
                    )
                    for trade in trades[-10:]
                ],
            ),
            strategy_recommendation_context=recommendation_context,
            fallback_observations=[],
        )
        report.fallback_observations = self._build_fallback_observations(report)
        return report

    def render_markdown(self, report: AnalysisReport) -> str:
        context = report.run_context
        performance = report.performance
        trade_breakdown = report.trade_breakdown
        risk = report.risk_diagnostics
        signals = report.signal_diagnostics

        lines = [
            "# Autobit Run Analysis Report",
            "",
            "## Run Context",
            f"- Run ID: `{context.run_id}`",
            f"- Status: `{context.status}`",
            f"- Data source: `{context.data_source}`",
            f"- Symbol: `{context.symbol}`",
            f"- Started at: `{context.started_at.isoformat()}`",
            f"- Ended at: `{context.ended_at.isoformat() if context.ended_at else 'n/a'}`",
            f"- Duration (sec): `{self._format_optional(context.duration_sec)}`",
            "",
            "## Performance",
            f"- Starting capital (TWD): `{performance.starting_capital_twd:.2f}`",
            f"- Ending value (TWD): `{performance.ending_value_twd:.2f}`",
            f"- PnL (TWD): `{performance.pnl_twd:.2f}`",
            f"- PnL (%): `{performance.pnl_pct:.2f}`",
            f"- Max drawdown (%): `{performance.max_drawdown_pct:.2f}`",
            f"- Total fee (TWD): `{performance.total_fee_twd:.2f}`",
            f"- Win rate (%): `{performance.win_rate_pct:.2f}`",
            f"- Trade count: `{performance.trade_count}`",
            f"- Tick count: `{performance.tick_count}`",
            "",
            "## Trade Breakdown",
            f"- Buy count: `{trade_breakdown.buy_count}`",
            f"- Sell count: `{trade_breakdown.sell_count}`",
            f"- Round trips: `{trade_breakdown.round_trip_count}`",
            f"- Avg round-trip PnL (TWD): `{self._format_optional(trade_breakdown.average_round_trip_pnl_twd)}`",
            f"- Avg winning trade (TWD): `{self._format_optional(trade_breakdown.average_winning_round_trip_pnl_twd)}`",
            f"- Avg losing trade (TWD): `{self._format_optional(trade_breakdown.average_losing_round_trip_pnl_twd)}`",
            f"- Avg holding minutes: `{self._format_optional(trade_breakdown.average_holding_minutes)}`",
            "",
            "## Signal Diagnostics",
            f"- BUY signals: `{signals.buy_signal_count}`",
            f"- SELL signals: `{signals.sell_signal_count}`",
            f"- HOLD signals: `{signals.hold_signal_count}`",
            f"- Latest signal: `{signals.latest_signal or 'n/a'}`",
            "",
            "### Top HOLD Reasons",
        ]

        if signals.top_hold_reasons:
            lines.extend(
                [
                    f"- {item.reason} | count={item.count} | ratio={item.ratio_pct:.2f}%"
                    for item in signals.top_hold_reasons
                ]
            )
        else:
            lines.append("- n/a")

        lines.extend(["", "### Top SELL Reasons"])
        if signals.top_sell_reasons:
            lines.extend(
                [
                    f"- {item.reason} | count={item.count} | ratio={item.ratio_pct:.2f}%"
                    for item in signals.top_sell_reasons
                ]
            )
        else:
            lines.append("- n/a")

        lines.extend(
            [
                "",
                "## Risk Diagnostics",
                f"- Stop-loss triggered: `{risk.stop_loss_trigger_count}`",
                f"- Trailing-stop triggered: `{risk.trailing_stop_trigger_count}`",
                f"- Fee to PnL ratio (%): `{self._format_optional(risk.fee_to_pnl_ratio_pct)}`",
                "",
                "## Fallback Observations",
            ]
        )
        if report.fallback_observations:
            lines.extend([f"- {item.title}: {item.detail}" for item in report.fallback_observations])
        else:
            lines.append("- No local fallback observation triggered.")

        lines.extend(["", "## Strategy Parameters"])
        lines.extend([f"- {key}: `{value}`" for key, value in context.strategy_params.items()])
        return "\n".join(lines)

    def build_prompt(self, report: AnalysisReport, markdown: str) -> str:
        compact_json = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
        return "\n".join(
            [
                "請使用以下 Autobit 交易報告進行分析。",
                "任務：",
                "1. 判讀這次 run 的績效與風險。",
                "2. 指出策略的主要問題與可能成因。",
                "3. 提出具體策略修正建議，且只可使用既有參數欄位。",
                "4. 補充下一輪回測應驗證的測試清單。",
                "",
                "請先輸出可閱讀的 Markdown 分析，最後再輸出一段 ```json``` 區塊。",
                "JSON 物件格式必須為：",
                "{",
                '  "summary": "string",',
                '  "observations": [{"title": "string", "detail": "string", "severity": "info|warning|critical"}],',
                '  "recommendations": [',
                '    {',
                '      "parameter": "rsi_entry_low|rsi_entry_high|rsi_exit_high|anti_chase_pct|stop_loss_pct|trail_trigger_pct|trail_stop_pct|ema_trend_period|ema_signal_period|rsi_period|macd_fast|macd_slow|macd_signal_line",',
                '      "current_value": 0,',
                '      "suggested_change": "increase|decrease|keep|test",',
                '      "suggested_value": 0,',
                '      "reason": "string",',
                '      "expected_effect": "string",',
                '      "confidence": "low|medium|high"',
                "    }",
                "  ],",
                '  "test_plan": ["string"]',
                "}",
                "",
                "分析報告 Markdown：",
                markdown,
                "",
                "關鍵 JSON 報告：",
                "```json",
                compact_json,
                "```",
            ]
        )

    def _build_trade_breakdown(
        self, round_trips: list[RoundTripSummary], trades: list[Any]
    ) -> AnalysisTradeBreakdown:
        winning = [trade.pnl_twd for trade in round_trips if trade.pnl_twd > 0]
        losing = [trade.pnl_twd for trade in round_trips if trade.pnl_twd < 0]
        average_holding = None
        if round_trips:
            holding_values = [trip.holding_minutes for trip in round_trips if trip.holding_minutes is not None]
            average_holding = sum(holding_values) / len(holding_values) if holding_values else None

        return AnalysisTradeBreakdown(
            buy_count=sum(1 for trade in trades if trade.action == "BUY"),
            sell_count=sum(1 for trade in trades if trade.action == "SELL"),
            round_trip_count=len(round_trips),
            average_round_trip_pnl_twd=(sum(trade.pnl_twd for trade in round_trips) / len(round_trips)) if round_trips else None,
            average_winning_round_trip_pnl_twd=(sum(winning) / len(winning)) if winning else None,
            average_losing_round_trip_pnl_twd=(sum(losing) / len(losing)) if losing else None,
            average_holding_minutes=average_holding,
            best_round_trip=max(round_trips, key=lambda trade: trade.pnl_twd) if round_trips else None,
            worst_round_trip=min(round_trips, key=lambda trade: trade.pnl_twd) if round_trips else None,
        )

    def _pair_round_trips(self, trades: list[Any]) -> list[RoundTripSummary]:
        round_trips: list[RoundTripSummary] = []
        pending_buy = None
        for trade in trades:
            if trade.action == "BUY":
                pending_buy = trade
                continue
            if trade.action != "SELL" or pending_buy is None:
                continue

            buy_cost_twd = self._convert_trade_amount_to_twd(pending_buy, "gross_usdt")
            sell_net_twd = self._convert_trade_amount_to_twd(trade, "net_usdt")
            pnl_twd = sell_net_twd - buy_cost_twd
            pnl_pct = (pnl_twd / buy_cost_twd * 100) if buy_cost_twd else 0.0

            entry_time = pending_buy.market_timestamp or pending_buy.timestamp
            exit_time = trade.market_timestamp or trade.timestamp
            holding_minutes = None
            if entry_time and exit_time:
                holding_minutes = max(0.0, (exit_time - entry_time).total_seconds() / 60)

            round_trips.append(
                RoundTripSummary(
                    entry_time=entry_time,
                    exit_time=exit_time,
                    holding_minutes=holding_minutes,
                    pnl_twd=pnl_twd,
                    pnl_pct=pnl_pct,
                    entry_reason=pending_buy.reason,
                    exit_reason=trade.reason,
                )
            )
            pending_buy = None
        return round_trips

    def _convert_trade_amount_to_twd(self, trade: Any, field: str) -> float:
        fx_rate = self._infer_trade_fx_rate(trade)
        return float(getattr(trade, field, 0.0) or 0.0) * fx_rate

    def _infer_trade_fx_rate(self, trade: Any) -> float:
        if trade.price and trade.price_twd:
            return trade.price_twd / trade.price
        if trade.fee_usdt and trade.fee_twd:
            return trade.fee_twd / trade.fee_usdt
        if trade.portfolio_value_after and trade.portfolio_value_after_twd:
            return trade.portfolio_value_after_twd / trade.portfolio_value_after
        return 1.0

    def _build_drawdown_segments(self, ticks: list[Any], *, threshold: float = 5.0) -> list[DrawdownSegment]:
        segments: list[DrawdownSegment] = []
        active_ticks: list[Any] = []

        for tick in ticks:
            drawdown = self._safe_float((tick.portfolio or {}).get("max_drawdown_pct"))
            if drawdown >= threshold:
                active_ticks.append(tick)
                continue
            if active_ticks:
                segments.append(self._segment_from_ticks(active_ticks))
                active_ticks = []

        if active_ticks:
            segments.append(self._segment_from_ticks(active_ticks))

        segments.sort(key=lambda item: item.max_drawdown_pct, reverse=True)
        return segments[:5]

    def _segment_from_ticks(self, ticks: list[Any]) -> DrawdownSegment:
        return DrawdownSegment(
            start_at=self._get_tick_time(ticks[0]),
            end_at=self._get_tick_time(ticks[-1]),
            max_drawdown_pct=max(self._safe_float((tick.portfolio or {}).get("max_drawdown_pct")) for tick in ticks),
            tick_count=len(ticks),
        )

    def _build_fallback_observations(self, report: AnalysisReport) -> list[AnalysisObservation]:
        observations: list[AnalysisObservation] = []
        performance = report.performance
        signals = report.signal_diagnostics
        risks = report.risk_diagnostics
        trade_breakdown = report.trade_breakdown
        dominant_hold = report.strategy_recommendation_context.dominant_hold_reason or ""

        if (
            performance.trade_count <= 2
            and signals.hold_signal_count > 0
            and report.strategy_recommendation_context.dominant_hold_reason_ratio_pct >= 35
            and self._matches_any(dominant_hold, ENTRY_FILTER_HINTS)
        ):
            observations.append(
                AnalysisObservation(
                    title="進場條件可能過嚴",
                    detail="交易次數偏低，且 HOLD 原因集中在趨勢或 RSI 過濾，建議檢查進場門檻是否太保守。",
                    category="entry",
                    severity="warning",
                    evidence=[dominant_hold],
                )
            )

        if performance.win_rate_pct < 45 and risks.stop_loss_trigger_count >= 1:
            observations.append(
                AnalysisObservation(
                    title="停損可能過寬或進場品質不足",
                    detail="勝率偏低且停損觸發頻繁，代表虧損單未能有效避免，應重新檢查停損參數或進場條件。",
                    category="risk",
                    severity="warning",
                    evidence=[f"win_rate={performance.win_rate_pct:.2f}", f"stop_loss_count={risks.stop_loss_trigger_count}"],
                )
            )

        if performance.win_rate_pct >= 55 and performance.pnl_twd <= performance.total_fee_twd * 1.2:
            observations.append(
                AnalysisObservation(
                    title="出場過早或手續費侵蝕偏高",
                    detail="雖然勝率不差，但整體獲利不足以拉開與手續費的差距，應檢查出場條件與交易密度。",
                    category="exit",
                    severity="warning",
                    evidence=[f"fee={performance.total_fee_twd:.2f}", f"pnl={performance.pnl_twd:.2f}"],
                )
            )

        if performance.max_drawdown_pct >= 10 and risks.trailing_stop_trigger_count == 0:
            observations.append(
                AnalysisObservation(
                    title="保護獲利機制偏弱",
                    detail="最大回撤偏高，但移動停利幾乎未發揮作用，可能需要更早啟動或縮小回撤空間。",
                    category="risk",
                    severity="warning",
                    evidence=[f"max_drawdown={performance.max_drawdown_pct:.2f}"],
                )
            )

        if risks.fee_to_pnl_ratio_pct is not None and risks.fee_to_pnl_ratio_pct >= 40:
            observations.append(
                AnalysisObservation(
                    title="交易過密或訊號品質不足",
                    detail="手續費占損益比例偏高，代表交易成本正在明顯吃掉績效，應降低無效訊號。",
                    category="cost",
                    severity="warning",
                    evidence=[f"fee_to_pnl_ratio={risks.fee_to_pnl_ratio_pct:.2f}%"],
                )
            )

        if not observations and trade_breakdown.round_trip_count == 0:
            observations.append(
                AnalysisObservation(
                    title="樣本不足",
                    detail="目前沒有完整 round-trip 交易，建議先延長回測區間或提高可成交機會，再做 AI 調參。",
                    category="sampling",
                    severity="info",
                )
            )
        return observations

    def _estimate_entry_filter_tightness(
        self, diagnostics: AnalysisSignalDiagnostics, performance: AnalysisPerformance
    ) -> str:
        if not diagnostics.top_hold_reasons:
            return "medium"
        dominant = diagnostics.top_hold_reasons[0]
        if performance.trade_count <= 2 and dominant.ratio_pct >= 35 and self._matches_any(dominant.reason, ENTRY_FILTER_HINTS):
            return "high"
        if dominant.ratio_pct <= 20:
            return "low"
        return "medium"

    def _estimate_exit_efficiency(self, performance: AnalysisPerformance, risks: AnalysisRiskDiagnostics) -> str:
        if performance.pnl_pct <= 0 or performance.max_drawdown_pct >= 12:
            return "weak"
        if performance.win_rate_pct >= 60 and (risks.fee_to_pnl_ratio_pct or 0) < 25:
            return "strong"
        return "mixed"

    def _reason_stats(self, reasons: list[str], *, limit: int = 5) -> list[AnalysisReasonStat]:
        filtered = [reason.strip() for reason in reasons if reason and reason.strip()]
        if not filtered:
            return []
        counter = Counter(filtered)
        total = sum(counter.values())
        return [
            AnalysisReasonStat(reason=reason, count=count, ratio_pct=count / total * 100)
            for reason, count in counter.most_common(limit)
        ]

    def _get_tick_time(self, tick: Any) -> datetime | None:
        if tick is None:
            return None
        return tick.market_timestamp or tick.timestamp

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _matches_any(self, value: str | None, needles: tuple[str, ...]) -> bool:
        text = (value or "").lower()
        return any(needle.lower() in text for needle in needles)

    def _format_optional(self, value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"
