from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredUpload:
    original_name: str
    stored_path: Path
    sha256: str
    size_bytes: int


def sha256_for_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_uploaded_files(
    *, run_id: str, files, workspace_root: Path
) -> list[StoredUpload]:
    raw_dir = workspace_root / "uploads" / run_id / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    stored: list[StoredUpload] = []

    for uploaded in files:
        safe_name = Path(uploaded.name).name
        target = raw_dir / safe_name

        with target.open("wb") as out_f:
            for chunk in uploaded.chunks():
                out_f.write(chunk)

        stored.append(
            StoredUpload(
                original_name=safe_name,
                stored_path=target,
                sha256=sha256_for_file(target),
                size_bytes=target.stat().st_size,
            )
        )

    return stored
