from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from etl.pipeline_service import run_pipeline as core_run_pipeline
from runs.models import AnalysisRun, RunArtifact, RunInput
from runs.services.storage_service import store_uploaded_files

from etl.run_config import sha256_file


def _copy_run_config(
    *,
    run_id: str,
    enable_cohort_mode: bool,
    project_root: Path,
) -> Path:
    source_dir = project_root / "config"
    target_dir = project_root / "workspace" / "uploads" / run_id / "config"

    shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

    cohorts_path = target_dir / "cohorts.json"
    if cohorts_path.exists():
        data = json.loads(cohorts_path.read_text(encoding="utf-8"))
        data["enabled"] = bool(enable_cohort_mode)
        cohorts_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return target_dir


def _normalise_name(value: str) -> str:
    return Path(value).name.strip().lower()


def _create_artifact(run: AnalysisRun, artifact_type: str, path: Path) -> None:
    RunArtifact.objects.create(
        run=run,
        artifact_type=artifact_type,
        name=path.name,
        relative_path=str(path),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        is_downloadable=True,
    )


def _record_artifacts(run: AnalysisRun, out_dir: Path) -> None:
    RunArtifact.objects.filter(run=run).delete()

    report_pdf = out_dir / "report_pack.pdf"
    if report_pdf.exists():
        _create_artifact(run, RunArtifact.ArtifactType.REPORT_PDF, report_pdf)

    bundle_zip = out_dir / "report_pack_bundle.zip"
    if bundle_zip.exists():
        _create_artifact(run, RunArtifact.ArtifactType.REPORT_BUNDLE, bundle_zip)

    run_config = out_dir / "run_config.json"
    if run_config.exists():
        _create_artifact(run, RunArtifact.ArtifactType.RUN_CONFIG, run_config)

    for csv_path in sorted(out_dir.glob("*.csv")):
        _create_artifact(run, RunArtifact.ArtifactType.METRICS_CSV, csv_path)

    charts_dir = out_dir / "charts"
    if charts_dir.exists():
        for png_path in sorted(charts_dir.glob("*.png")):
            _create_artifact(run, RunArtifact.ArtifactType.CHART, png_path)


def create_analysis_run(
    *,
    run_label: str,
    uploaded_files,
    enable_cohort_mode: bool,
    project_root: Path,
) -> AnalysisRun:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    config_dir = _copy_run_config(
        run_id=run_id,
        enable_cohort_mode=enable_cohort_mode,
        project_root=project_root,
    )

    run = AnalysisRun.objects.create(
        run_id=run_id,
        label=run_label.strip(),
        status=AnalysisRun.Status.QUEUED,
        source_mode=AnalysisRun.SourceMode.UPLOAD,
        cohort_enabled=enable_cohort_mode,
        config_dir=str(config_dir),
        out_dir=str(project_root / "out" / f"run_{run_id}"),
        warnings_count=0,
        finished_at=None,
    )

    stored_files = store_uploaded_files(
        run_id=run_id,
        files=uploaded_files,
        workspace_root=project_root / "workspace",
    )

    for stored in stored_files:
        RunInput.objects.create(
            run=run,
            original_name=stored.original_name,
            stored_path=str(stored.stored_path),
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            detected_variant=RunInput.Variant.UNKNOWN,
            accepted=False,
            message="Uploaded and queued for processing.",
        )

    return run


def execute_analysis_run(
    *,
    run: AnalysisRun,
    build_bundle: bool,
    project_root: Path,
) -> AnalysisRun:
    run.status = AnalysisRun.Status.RUNNING
    run.error_message = ""
    run.save(update_fields=["status", "error_message"])

    try:
        raw_files = [Path(item.stored_path) for item in run.inputs.all()]

        result = core_run_pipeline(
            project_root=project_root,
            raw_files=raw_files,
            config_dir=Path(run.config_dir),
            out_root=project_root / "out",
            run_id=run.run_id,
            build_bundle=build_bundle,
        )

        accepted_names = {_normalise_name(x) for x in result.accepted_files}
        rejected_names = {_normalise_name(x) for x in result.rejected_files}

        for item in run.inputs.all():
            name_key = _normalise_name(item.original_name)
            if name_key in accepted_names:
                item.accepted = True
                item.message = "Accepted and processed successfully."
            elif name_key in rejected_names:
                item.accepted = False
                item.message = "Rejected during validation."
            else:
                item.message = "Processed. Final status not explicitly returned."
            item.save(update_fields=["accepted", "message"])

        run.status = AnalysisRun.Status.COMPLETED
        run.finished_at = timezone.now()
        run.out_dir = str(result.out_dir)
        run.run_config_path = str(result.run_config_path)
        run.accepted_files_count = len(result.accepted_files)
        run.rejected_files_count = len(result.rejected_files)
        run.warnings_count = (
            len(result.ingest_warnings)
            + len(result.normalise_warnings)
            + len(result.fr4_warnings)
            + len(result.fr5_warnings)
        )
        run.normalised_rows = result.normalised_rows
        run.normalised_cols = result.normalised_cols
        run.cohort_enabled = result.cohort_enabled
        run.stage_summary_json = {
            "charts_written": result.charts_written,
            "report_pdf": str(result.report_pdf) if result.report_pdf else "",
            "bundle_path": str(result.bundle_path) if result.bundle_path else "",
        }
        run.save()

        _record_artifacts(run, result.out_dir)

    except Exception as exc:
        run.status = AnalysisRun.Status.FAILED
        run.finished_at = timezone.now()
        run.error_message = str(exc)
        run.save(update_fields=["status", "finished_at", "error_message"])

    return run
