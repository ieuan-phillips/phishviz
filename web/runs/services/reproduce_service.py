from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.utils import timezone

from etl.pipeline_service import run_pipeline as core_run_pipeline
from etl.run_config import load_run_config, validate_inputs_against_run_config
from runs.models import AnalysisRun, RunArtifact, RunInput

from etl.run_config import sha256_file


@dataclass(frozen=True)
class RunVerificationResult:
    run_config_path: Path
    run_config_exists: bool
    is_valid: bool
    problems: list[str]
    run_id_from_config: str
    started_utc: str
    finished_utc: str
    snapshot_dir: str
    referenced_inputs: list[dict[str, Any]]
    generated_outputs: list[dict[str, Any]]


def _resolve_project_path(project_root: Path, path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


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


def verify_run_against_config(
    *,
    run_config_path: Path,
    project_root: Path,
) -> RunVerificationResult:
    run_config_path = run_config_path.resolve()
    project_root = project_root.resolve()

    if not run_config_path.exists():
        return RunVerificationResult(
            run_config_path=run_config_path,
            run_config_exists=False,
            is_valid=False,
            problems=["run_config.json was not found for this run."],
            run_id_from_config="",
            started_utc="",
            finished_utc="",
            snapshot_dir="",
            referenced_inputs=[],
            generated_outputs=[],
        )

    run_config = load_run_config(run_config_path)

    problems = validate_inputs_against_run_config(
        run_config,
        project_root=project_root,
    )

    return RunVerificationResult(
        run_config_path=run_config_path,
        run_config_exists=True,
        is_valid=(len(problems) == 0),
        problems=problems,
        run_id_from_config=str(run_config.get("run", {}).get("id", "")),
        started_utc=str(run_config.get("run", {}).get("started_utc", "")),
        finished_utc=str(run_config.get("run", {}).get("finished_utc", "")),
        snapshot_dir=str(run_config.get("config", {}).get("snapshot_dir", "")),
        referenced_inputs=list(run_config.get("inputs", {}).get("raw_files", [])),
        generated_outputs=list(
            run_config.get("outputs", {}).get("generated_files", [])
        ),
    )


def create_reproduce_run(
    *,
    source_run: AnalysisRun,
    project_root: Path,
) -> AnalysisRun:
    source_config_path = Path(source_run.run_config_path)
    run_config = load_run_config(source_config_path)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = str(run_config.get("config", {}).get("snapshot_dir", ""))
    resolved_config_dir = (
        _resolve_project_path(project_root, snapshot_dir)
        if snapshot_dir
        else (project_root / "config")
    )

    run = AnalysisRun.objects.create(
        run_id=run_id,
        label=f"Reproduce of {source_run.run_id}",
        status=AnalysisRun.Status.QUEUED,
        source_mode=AnalysisRun.SourceMode.REPRODUCE,
        cohort_enabled=source_run.cohort_enabled,
        config_dir=str(resolved_config_dir),
        out_dir=str(project_root / "out" / f"run_{run_id}"),
        warnings_count=0,
        finished_at=None,
        stage_summary_json={"reproduced_from": source_run.run_id},
    )

    for item in run_config.get("inputs", {}).get("raw_files", []):
        stored_path = _resolve_project_path(project_root, str(item.get("path", "")))
        RunInput.objects.create(
            run=run,
            original_name=str(item.get("name", Path(stored_path).name)),
            stored_path=str(stored_path),
            sha256=str(item.get("sha256", "")),
            size_bytes=int(item.get("size_bytes", 0) or 0),
            detected_variant=RunInput.Variant.UNKNOWN,
            accepted=False,
            message="Referenced from saved run configuration.",
        )

    return run


def execute_reproduce_run(
    *,
    run: AnalysisRun,
    source_run: AnalysisRun,
    project_root: Path,
) -> AnalysisRun:
    run.status = AnalysisRun.Status.RUNNING
    run.error_message = ""
    run.save(update_fields=["status", "error_message"])

    try:
        result = core_run_pipeline(
            project_root=project_root,
            config_dir=Path(run.config_dir),
            out_root=project_root / "out",
            run_id=run.run_id,
            reproduce_from=Path(source_run.run_config_path),
            allow_input_mismatch=False,
            build_bundle=True,
        )

        accepted_names = {_normalise_name(x) for x in result.accepted_files}
        rejected_names = {_normalise_name(x) for x in result.rejected_files}

        for item in run.inputs.all():
            name_key = _normalise_name(item.original_name)
            if name_key in accepted_names:
                item.accepted = True
                item.message = "Accepted and reproduced successfully."
            elif name_key in rejected_names:
                item.accepted = False
                item.message = "Rejected during reproduce validation."
            else:
                item.message = "Processed in reproduce mode."
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
            "reproduced_from": source_run.run_id,
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
