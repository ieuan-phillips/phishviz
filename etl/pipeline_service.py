from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from etl.ingest import load_ast_exports
from etl.normalise import normalise_results
from etl.metrics import (
    compute_metrics_fr4,
    write_metrics,
    load_cohort_config,
    load_cohort_map_csv,
    compute_metrics_fr5,
    write_cohort_metrics,
)
from etl.visualise import render_fr6_charts
from etl.report_pack import render_fr7_report_pack, build_fr7_bundle
from etl.run_config import (
    build_run_config_initial,
    update_stage,
    finalise_outputs,
    load_run_config,
    validate_inputs_against_run_config,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineRunResult:
    run_id: str
    out_dir: Path
    run_config_path: Path
    accepted_files: list[str] = field(default_factory=list)
    rejected_files: list[str] = field(default_factory=list)
    ingest_warnings: list[str] = field(default_factory=list)
    normalise_warnings: list[str] = field(default_factory=list)
    fr4_warnings: list[str] = field(default_factory=list)
    fr5_warnings: list[str] = field(default_factory=list)
    normalised_rows: int = 0
    normalised_cols: int = 0
    charts_written: int = 0
    report_pdf: Optional[Path] = None
    bundle_path: Optional[Path] = None
    cohort_enabled: bool = False


def resolve_project_path(project_root: Path, path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (project_root / p).resolve()


def run_pipeline(
    *,
    project_root: Path,
    raw_dir: Optional[Path] = None,
    raw_files: Optional[list[Path]] = None,
    config_dir: Path,
    out_root: Path,
    run_id: Optional[str] = None,
    reproduce_from: Optional[Path] = None,
    allow_input_mismatch: bool = False,
    build_bundle: bool = True,
) -> PipelineRunResult:
    """
    Core PhishViz pipeline runner used by both CLI and, later, Django.
    """
    project_root = project_root.resolve()
    raw_dir = raw_dir.resolve() if raw_dir else None
    config_dir = config_dir.resolve()
    out_root = out_root.resolve()

    raw_files = [p.resolve() for p in (raw_files or [])]

    # -------------------------
    # Reproduce mode (optional)
    # -------------------------
    if reproduce_from:
        run_config = load_run_config(reproduce_from.resolve())

        problems = validate_inputs_against_run_config(
            run_config, project_root=project_root
        )
        if not problems:
            logger.info("FR8 validation: input hashes match run_config.json")
        if problems and not allow_input_mismatch:
            raise ValueError(
                "Input validation failed against run_config.json:\n- "
                + "\n- ".join(problems)
            )

        snap = str(run_config.get("config", {}).get("snapshot_dir", ""))
        snap_dir = resolve_project_path(project_root, snap) if snap else None
        if snap_dir and snap_dir.exists():
            config_dir = snap_dir
        else:
            config_dir = resolve_project_path(
                project_root,
                str(run_config.get("config", {}).get("config_dir", str(config_dir))),
            )

        raw_entries = run_config.get("inputs", {}).get("raw_files", [])
        raw_files = [
            resolve_project_path(project_root, str(e.get("path", "")))
            for e in raw_entries
            if e.get("path")
        ]

        base_id = str(run_config.get("run", {}).get("id", "")).strip() or "repro"
        effective_run_id = (
            run_id or f"{base_id}_repro_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        effective_raw_dir = None
    else:
        if not raw_files:
            if raw_dir is None:
                raise ValueError("Either raw_files or raw_dir must be provided.")
            raw_files = sorted(raw_dir.glob("*.csv"))

        if not raw_files:
            raise ValueError(f"No CSV exports found in: {raw_dir}")

        effective_run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        effective_raw_dir = raw_dir

    out_dir = out_root / f"run_{effective_run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------
    # FR8: run configuration
    # -------------------------
    rc = build_run_config_initial(
        project_root=project_root,
        run_id=effective_run_id,
        out_dir=out_dir,
        raw_files=raw_files,
        raw_dir=effective_raw_dir,
        config_dir=config_dir,
    )

    # -------------------------
    # Load config (FR5 controls)
    # -------------------------
    cohort_config = load_cohort_config(str(config_dir / "cohorts.json"))
    cohort_map = load_cohort_map_csv(str(config_dir / "cohort_map.csv"))

    # -------------------------
    # Ingest + normalise
    # -------------------------
    ingest_result = load_ast_exports([str(p) for p in raw_files])
    update_stage(
        rc,
        "ingest",
        {
            "accepted_files": ingest_result.accepted_files,
            "rejected_files": ingest_result.rejected_files,
            "warnings_count": len(getattr(ingest_result, "warnings", [])),
        },
        out_dir=out_dir,
    )

    if not ingest_result.accepted_files:
        raise ValueError(
            "No valid AST exports were accepted during ingestion. "
            "Review the rejected file warnings and validation errors."
        )

    normalise_result = normalise_results(ingest_result.df)

    update_stage(
        rc,
        "normalise",
        {
            "rows": int(len(normalise_result.df)),
            "cols": int(len(normalise_result.df.columns)),
            "warnings_count": int(len(getattr(normalise_result, "warnings", []))),
        },
        out_dir=out_dir,
    )

    # -------------------------
    # FR4 metrics + tables
    # -------------------------
    metrics_fr4 = compute_metrics_fr4(normalise_result.df)
    write_metrics(metrics_fr4, str(out_dir))
    update_stage(
        rc,
        "fr4",
        {"warnings_count": int(len(getattr(metrics_fr4, "warnings", [])))},
        out_dir=out_dir,
    )

    # -------------------------
    # FR5 cohort metrics (optional)
    # -------------------------
    metrics_fr5 = None
    fr5_warnings: list[str] = []
    cohort_enabled = bool(cohort_config.get("enabled", False))

    if cohort_enabled:
        metrics_fr5 = compute_metrics_fr5(
            normalise_result.df,
            cohort_config=cohort_config,
            cohort_map=cohort_map,
        )
        write_cohort_metrics(metrics_fr5, str(out_dir))
        fr5_warnings = list(getattr(metrics_fr5, "warnings", []))

        update_stage(
            rc,
            "fr5",
            {
                "enabled": True,
                "warnings_count": int(len(fr5_warnings)),
            },
            out_dir=out_dir,
        )

        for w in fr5_warnings[:20]:
            logger.warning("FR5: %s", w)
    else:
        update_stage(rc, "fr5", {"enabled": False}, out_dir=out_dir)

    # -------------------------
    # FR6 charts
    # -------------------------
    charts = render_fr6_charts(
        fr4_campaign_metrics=metrics_fr4.campaign_metrics,
        fr4_time_metrics=metrics_fr4.time_metrics,
        fr4_repeat_metrics=metrics_fr4.repeat_metrics,
        fr5_cohort_trend_metrics=(
            metrics_fr5.cohort_trend_metrics if metrics_fr5 else None
        ),
        fr5_cohort_comparison_metrics=(
            metrics_fr5.cohort_comparison_metrics if metrics_fr5 else None
        ),
        out_dir=str(out_dir),
    )

    # -------------------------
    # FR7 report pack
    # -------------------------
    report_pdf = render_fr7_report_pack(
        out_dir=out_dir,
        campaign_metrics=metrics_fr4.campaign_metrics,
        build_zip_bundle=False,
    )

    bundle_path: Optional[Path] = None
    if build_bundle:
        bundle_path = build_fr7_bundle(out_dir=out_dir)

    # -------------------------
    # FR8 finalise outputs
    # -------------------------
    finalise_outputs(
        rc,
        project_root=project_root,
        out_dir=out_dir,
        exclude_paths=set(),
    )

    return PipelineRunResult(
        run_id=effective_run_id,
        out_dir=out_dir,
        run_config_path=out_dir / "run_config.json",
        accepted_files=ingest_result.accepted_files,
        rejected_files=ingest_result.rejected_files,
        ingest_warnings=list(getattr(ingest_result, "warnings", [])),
        normalise_warnings=list(getattr(normalise_result, "warnings", [])),
        fr4_warnings=list(getattr(metrics_fr4, "warnings", [])),
        fr5_warnings=fr5_warnings,
        normalised_rows=int(len(normalise_result.df)),
        normalised_cols=int(len(normalise_result.df.columns)),
        charts_written=len(charts),
        report_pdf=Path(report_pdf) if report_pdf else None,
        bundle_path=Path(bundle_path) if bundle_path else None,
        cohort_enabled=cohort_enabled,
    )
