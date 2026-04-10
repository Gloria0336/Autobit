from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from web_models import AnalyzeReportResponse, SaveReportArchiveRequest, SaveReportArchiveResponse


class ReportArchiveService:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_run_report(self, run_id: str, payload: SaveReportArchiveRequest) -> SaveReportArchiveResponse:
        saved_at = datetime.now(timezone.utc)
        run_dir = self.base_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        stamp = saved_at.strftime("%Y%m%dT%H%M%SZ")
        report_path = run_dir / f"{stamp}_report.md"
        json_path = run_dir / f"{stamp}_archive.json"
        latest_report_path = run_dir / "latest_report.md"
        latest_json_path = run_dir / "latest_archive.json"

        report_path.write_text(payload.markdown, encoding="utf-8")
        latest_report_path.write_text(payload.markdown, encoding="utf-8")

        archive_payload = {
            "run_id": run_id,
            "saved_at": saved_at.isoformat(),
            "report": payload.report.model_dump(mode="json"),
            "markdown": payload.markdown,
            "ai_analysis_markdown": payload.ai_analysis_markdown,
            "recommendations": [item.model_dump(mode="json") for item in payload.recommendations],
            "test_plan": list(payload.test_plan),
            "model": payload.model,
            "generated_at": payload.generated_at.isoformat() if payload.generated_at else None,
            "parsing_error": payload.parsing_error,
        }
        archive_json = json.dumps(archive_payload, ensure_ascii=False, indent=2)
        json_path.write_text(archive_json, encoding="utf-8")
        latest_json_path.write_text(archive_json, encoding="utf-8")

        saved_files = [str(report_path), str(json_path)]
        if payload.ai_analysis_markdown:
            analysis_path = run_dir / f"{stamp}_ai_analysis.md"
            latest_analysis_path = run_dir / "latest_ai_analysis.md"
            analysis_path.write_text(payload.ai_analysis_markdown, encoding="utf-8")
            latest_analysis_path.write_text(payload.ai_analysis_markdown, encoding="utf-8")
            saved_files.append(str(analysis_path))

        return SaveReportArchiveResponse(
            run_id=run_id,
            archive_dir=str(run_dir),
            saved_at=saved_at,
            saved_files=saved_files,
        )

    def save_analysis_response(self, run_id: str, payload: AnalyzeReportResponse) -> SaveReportArchiveResponse:
        return self.save_run_report(
            run_id,
            SaveReportArchiveRequest(
                report=payload.report,
                markdown=payload.markdown,
                ai_analysis_markdown=payload.ai_analysis_markdown,
                recommendations=payload.recommendations,
                test_plan=payload.test_plan,
                model=payload.model,
                generated_at=payload.generated_at,
                parsing_error=payload.parsing_error,
            ),
        )
