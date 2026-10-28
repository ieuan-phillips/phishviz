import argparse
import logging
from pathlib import Path

from etl.pipeline_service import run_pipeline, resolve_project_path

logging.basicConfig(level=logging.INFO)
logging.getLogger("matplotlib").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PhishViz: ETL + metrics + charts + report pack for Microsoft Defender AST exports"
    )
    p.add_argument(
        "--raw-dir",
        default="ast_exports/raw",
        help="Folder containing AST CSV exports (default: ast_exports/raw)",
    )
    p.add_argument(
        "--config-dir",
        default="config",
        help="Folder containing PhishViz configuration files (default: config)",
    )
    p.add_argument(
        "--out-root",
        default="out",
        help="Output root folder (default: out)",
    )
    p.add_argument(
        "--run-id",
        default="",
        help="Optional run identifier (default: timestamp)",
    )
    p.add_argument(
        "--run-config",
        default="",
        help="Path to an existing out/run_*/run_config.json to reproduce/verify a run",
    )
    p.add_argument(
        "--no-bundle",
        action="store_true",
        help="Do not create report_pack_bundle.zip",
    )
    p.add_argument(
        "--allow-input-mismatch",
        action="store_true",
        help="When using --run-config, continue even if input hashes differ (not recommended)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parent

    reproduce_from = (
        resolve_project_path(project_root, args.run_config) if args.run_config else None
    )

    if reproduce_from:
        raw_dir = None
        raw_files = None
        config_dir = resolve_project_path(project_root, args.config_dir)
    else:
        raw_dir = resolve_project_path(project_root, args.raw_dir)
        raw_files = sorted(raw_dir.glob("*.csv"))
        config_dir = resolve_project_path(project_root, args.config_dir)

    result = run_pipeline(
        project_root=project_root,
        raw_dir=raw_dir,
        raw_files=raw_files,
        config_dir=config_dir,
        out_root=resolve_project_path(project_root, args.out_root),
        run_id=args.run_id.strip() or None,
        reproduce_from=reproduce_from,
        allow_input_mismatch=args.allow_input_mismatch,
        build_bundle=not args.no_bundle,
    )

    print("FR6 charts written:", result.charts_written)
    if result.report_pdf:
        print(f"FR7 report pack written: {result.report_pdf}")
    if result.bundle_path:
        print(f"FR7 bundle written: {result.bundle_path}")

    print("Accepted:", len(result.accepted_files))
    print("Rejected:", len(result.rejected_files))
    print("Ingest warnings:", len(result.ingest_warnings))
    print("Normalised rows:", result.normalised_rows, "cols:", result.normalised_cols)
    print("Outputs written to:", result.out_dir)


if __name__ == "__main__":
    main()
