from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from importlib import metadata as importlib_metadata  # Python 3.8+
except Exception:  # pragma: no cover
    import importlib_metadata  # type: ignore


SCHEMA_VERSION = "1.0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _safe_relpath(path: Path, project_root: Path) -> str:
    """
    Prefer a project-relative, POSIX-style path for portability.
    Falls back to absolute path if relative conversion fails.
    """
    try:
        rel = path.resolve().relative_to(project_root.resolve())
        return rel.as_posix()
    except Exception:
        return path.resolve().as_posix()


def describe_file(path: Path, *, project_root: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "path": _safe_relpath(path, project_root),
        "name": path.name,
        "size_bytes": int(st.st_size),
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "sha256": sha256_file(path),
    }


def _get_git_commit(project_root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _get_packages(packages: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in packages:
        try:
            out[p] = importlib_metadata.version(p)
        except Exception:
            out[p] = ""
    return out


def _read_text_if_small(path: Path, *, max_bytes: int = 200_000) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def snapshot_directory(
    *,
    src_dir: Path,
    dest_dir: Path,
    project_root: Path,
    include_contents: bool = False,
) -> list[dict[str, Any]]:
    """
    Copies all regular files from src_dir into dest_dir (flat structure preserved).
    Returns file metadata (and optionally small file contents).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    files: list[Path] = [p for p in src_dir.rglob("*") if p.is_file()]
    files = sorted(files, key=lambda p: p.as_posix())

    manifest: list[dict[str, Any]] = []
    for p in files:
        rel = p.relative_to(src_dir)
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # copy2 keeps mtime which can be useful for audit trails
        try:
            import shutil

            shutil.copy2(p, target)
        except Exception:
            # If copying fails, still record the source metadata
            pass

        row = {
            "source_path": _safe_relpath(p, project_root),
            "snapshot_path": _safe_relpath(target, project_root),
            "sha256": sha256_file(p) if p.exists() else "",
            "size_bytes": int(p.stat().st_size) if p.exists() else 0,
        }
        if include_contents:
            row["contents_utf8"] = _read_text_if_small(p)
        manifest.append(row)

    return manifest


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_run_config_initial(
    *,
    project_root: Path,
    run_id: str,
    out_dir: Path,
    raw_files: list[Path],
    raw_dir: Optional[Path],
    config_dir: Path,
    extra_packages: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Creates the initial run_config structure, snapshots config, and writes run_config.json.
    """
    project_root = project_root.resolve()
    out_dir = out_dir.resolve()

    # Deterministic ordering
    raw_files = sorted([p.resolve() for p in raw_files], key=lambda p: p.as_posix())

    pkgs = ["pandas", "numpy", "matplotlib", "reportlab"]
    if extra_packages:
        pkgs.extend(extra_packages)

    config_snapshot_dir = out_dir / "config_snapshot"
    config_manifest = snapshot_directory(
        src_dir=config_dir,
        dest_dir=config_snapshot_dir,
        project_root=project_root,
        include_contents=False,
    )

    raw_manifest = [describe_file(p, project_root=project_root) for p in raw_files]

    run_config: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_id,
            "started_utc": _utc_now_iso(),
            "project_root": project_root.as_posix(),
            "git_commit": _get_git_commit(project_root),
        },
        "inputs": {
            "raw_dir": _safe_relpath(raw_dir, project_root) if raw_dir else "",
            "raw_files": raw_manifest,
        },
        "config": {
            "config_dir": _safe_relpath(config_dir, project_root),
            "snapshot_dir": _safe_relpath(config_snapshot_dir, project_root),
            "files": config_manifest,
        },
        "environment": {
            "python": {
                "version": sys.version.split()[0],
                "implementation": platform.python_implementation(),
            },
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "packages": _get_packages(pkgs),
        },
        "stages": {},
        "outputs": {
            "out_dir": _safe_relpath(out_dir, project_root),
            "generated_files": [],
        },
    }

    write_json(out_dir / "run_config.json", run_config)
    return run_config


def update_stage(
    run_config: dict[str, Any], stage: str, payload: dict[str, Any], *, out_dir: Path
) -> None:
    run_config.setdefault("stages", {})
    run_config["stages"][stage] = payload
    write_json(Path(out_dir) / "run_config.json", run_config)


def finalise_outputs(
    run_config: dict[str, Any],
    *,
    project_root: Path,
    out_dir: Path,
    exclude_paths: Optional[set[str]] = None,
) -> None:
    """
    Enumerate output files and attach hashes and sizes for auditability.
    Ensures run_config.json is hashed only after its final content is written.
    """
    project_root = project_root.resolve()
    out_dir = out_dir.resolve()
    exclude_paths = exclude_paths or set()

    generated: list[dict[str, Any]] = []
    run_config_rel = _safe_relpath(out_dir / "run_config.json", project_root)

    for p in sorted(out_dir.rglob("*"), key=lambda x: x.as_posix()):
        if not p.is_file():
            continue

        rel = _safe_relpath(p, project_root)

        if rel in exclude_paths:
            continue

        if rel == run_config_rel:
            continue

        # Avoid hashing raw exports if they were copied accidentally
        if "ast_exports" in rel.replace("\\", "/"):
            continue

        try:
            row = describe_file(p, project_root=project_root)
            generated.append(row)
        except Exception:
            continue

    run_config.setdefault("outputs", {})
    run_config["outputs"]["generated_files"] = generated
    run_config["run"]["finished_utc"] = _utc_now_iso()

    run_config_path = out_dir / "run_config.json"
    write_json(run_config_path, run_config)

    # Now hash the final written run_config.json and append it last
    try:
        run_config_row = describe_file(run_config_path, project_root=project_root)
        run_config["outputs"]["generated_files"].append(run_config_row)
    except Exception:
        pass

    write_json(run_config_path, run_config)


def load_run_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_inputs_against_run_config(
    run_config: dict[str, Any], *, project_root: Path
) -> list[str]:
    """
    Validates current input files against stored hashes. Returns a list of
    human-readable problems (empty means OK).
    """
    problems: list[str] = []
    project_root = project_root.resolve()

    expected = run_config.get("inputs", {}).get("raw_files", [])
    if not expected:
        problems.append("run_config has no inputs.raw_files entries")
        return problems

    for entry in expected:
        path_str = str(entry.get("path", ""))
        expected_hash = str(entry.get("sha256", ""))
        if not path_str:
            problems.append("input entry missing path")
            continue

        p = (project_root / path_str).resolve()
        if not p.exists():
            problems.append(f"missing input file: {path_str}")
            continue

        try:
            actual_hash = sha256_file(p)
        except Exception:
            problems.append(f"could not hash input file: {path_str}")
            continue

        if expected_hash and actual_hash.lower() != expected_hash.lower():
            problems.append(f"hash mismatch for {path_str}")
    return problems
