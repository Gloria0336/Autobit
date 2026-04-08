from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from analysis_report import RunReportService
from config import get_openrouter_settings
from openrouter_client import OpenRouterClient
from web_models import AnalyzeReportRequest, AnalyzeReportResponse, RunDetail, StrategyRecommendation


class ReportAnalysisService:
    def __init__(
        self,
        *,
        report_service: RunReportService | None = None,
        openrouter_client: OpenRouterClient | None = None,
        default_model: str = "",
    ) -> None:
        self.report_service = report_service or RunReportService()
        self.openrouter_client = openrouter_client or OpenRouterClient()
        self.default_model = default_model

    def analyze_run(
        self,
        detail: RunDetail,
        payload: AnalyzeReportRequest,
        *,
        referer: str | None = None,
        title: str | None = None,
    ) -> AnalyzeReportResponse:
        settings = get_openrouter_settings()
        model = (payload.model or self.default_model or settings["model"]).strip()
        run_report = self.report_service.build_response(detail, default_model=model)
        ai_result = self.openrouter_client.analyze(
            prompt=run_report.prompt,
            model=model,
            api_key=payload.api_key,
            referer=referer,
            title=title,
        )
        recommendations, test_plan, parsing_error = self._extract_payload(
            ai_result.content,
            max_observations=payload.max_observations,
            max_recommendations=payload.max_recommendations,
        )
        return AnalyzeReportResponse(
            report=run_report.report,
            markdown=run_report.markdown,
            ai_analysis_markdown=ai_result.content,
            recommendations=recommendations,
            test_plan=test_plan,
            model=ai_result.model,
            generated_at=datetime.now(timezone.utc),
            parsing_error=parsing_error,
        )

    def _extract_payload(
        self,
        markdown: str,
        *,
        max_observations: int,
        max_recommendations: int,
    ) -> tuple[list[StrategyRecommendation], list[str], str | None]:
        del max_observations
        payload, parse_error = self._parse_json_block(markdown)
        if payload is None:
            return [], [], parse_error

        recommendations: list[StrategyRecommendation] = []
        validation_errors: list[str] = []
        for item in (payload.get("recommendations") or [])[:max_recommendations]:
            try:
                recommendations.append(StrategyRecommendation.model_validate(item))
            except Exception as exc:
                validation_errors.append(str(exc))

        test_plan = [str(item) for item in (payload.get("test_plan") or []) if str(item).strip()]
        parsing_error = parse_error
        if validation_errors:
            parsing_error = "; ".join(filter(None, [parse_error, *validation_errors]))
        return recommendations, test_plan, parsing_error

    def _parse_json_block(self, markdown: str) -> tuple[dict[str, Any] | None, str | None]:
        block_match = re.search(r"```json\s*(\{.*?\})\s*```", markdown, re.DOTALL | re.IGNORECASE)
        candidate = block_match.group(1) if block_match else None
        if candidate is None:
            brace_match = re.search(r"(\{[\s\S]*\})", markdown)
            candidate = brace_match.group(1) if brace_match else None
        if candidate is None:
            return None, "AI response did not include a JSON block"
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError as exc:
            return None, f"Failed to parse AI JSON block: {exc}"
