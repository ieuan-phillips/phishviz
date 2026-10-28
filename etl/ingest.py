from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_MAX_MB = 50

_MISSING_TOKENS = {"", "--", "n/a", "na", "null", "none"}
_AST_RESULTS_TS_FORMAT = "%m/%d/%Y %I:%M:%S %p"


# ----------------------------
# Results schema (union of supported variants)
# ----------------------------

RESULTS_COMMON_COLUMNS: set[str] = {
    "username",
    "usermail",
    "compromised",
    "successfullydeliveredemail_timestamp",
    "failedtodeliveremail_timestamp",
    "messageread_timestamp",
    "messagedeleted_timestamp",
    "messagereplied_timestamp",
    "messageforwarded_timestamp",
    "outofofficedays",
    "positivereinforcementmessagedelivered_timestamp",
    "positivereinforcementmessagefailed_timestamp",
    "justsimulationmessagedelivered_timestamp",
    "justsimulationmessagefailed_timestamp",
    "trainingassignmentmessagedelivered_timestamp",
    "trainingassignmentmessagefailed_timestamp",
    "last_simulation_activity",
    "assigned_trainings",
    "completed_trainings",
    "training_status",
    "phishing_reported_on",
}

RESULTS_CRED_HARVEST_COLUMNS: set[str] = {
    "emaillinkclicked_timestamp",
    "emaillinkclicked_browser",
    "emaillinkclicked_clicksource",
    "emaillinkclicked_ip",
    "emaillinkclicked_device",
    "credsupplied_timestamp_compromised",
    "credsupplied_browser",
    "credsupplied_clicksource",
    "credsupplied_ip",
    "credsupplied_device",
}

RESULTS_MALWARE_ATTACH_COLUMNS: set[str] = {
    "attachmentopened_timestamp_compromised",
    "attachmentopened_browser",
    "attachmentopened_clicksource",
    "attachmentopened_ip",
    "attachmentopened_device",
}

RESULTS_ALLOWLIST_RAW: set[str] = (
    RESULTS_COMMON_COLUMNS
    | RESULTS_CRED_HARVEST_COLUMNS
    | RESULTS_MALWARE_ATTACH_COLUMNS
    | {"ref"}
)

RESULTS_REQUIRED_COMMON: set[str] = {
    "compromised",
    "successfullydeliveredemail_timestamp",
    "failedtodeliveremail_timestamp",
    "messageread_timestamp",
    "phishing_reported_on",
    "outofofficedays",
    "last_simulation_activity",
    "assigned_trainings",
    "completed_trainings",
    "training_status",
}

RESULTS_REQUIRED_BY_VARIANT: dict[str, set[str]] = {
    "credential_harvest": {
        "emaillinkclicked_timestamp",
        "credsupplied_timestamp_compromised",
    },
    "malware_attachment": {"attachmentopened_timestamp_compromised"},
}

RESULTS_TIMESTAMP_COLUMNS: tuple[str, ...] = (
    # Common timestamps
    "successfullydeliveredemail_timestamp",
    "failedtodeliveremail_timestamp",
    "messageread_timestamp",
    "messagedeleted_timestamp",
    "messagereplied_timestamp",
    "messageforwarded_timestamp",
    "positivereinforcementmessagedelivered_timestamp",
    "positivereinforcementmessagefailed_timestamp",
    "justsimulationmessagedelivered_timestamp",
    "justsimulationmessagefailed_timestamp",
    "trainingassignmentmessagedelivered_timestamp",
    "trainingassignmentmessagefailed_timestamp",
    "phishing_reported_on",
    # Credential harvest
    "emaillinkclicked_timestamp",
    "credsupplied_timestamp_compromised",
    # Malware attachment
    "attachmentopened_timestamp_compromised",
)

PROHIBITED_COLUMN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r".*message.*body.*"),
    re.compile(r".*email.*body.*"),
    re.compile(r".*body.*"),
    re.compile(r".*subject.*"),
    re.compile(r".*tenant.*id.*"),
    re.compile(r".*machine.*id.*"),
    re.compile(r".*session.*id.*"),
)


@dataclass(frozen=True)
class IngestResult:
    df: pd.DataFrame
    accepted_files: list[str]
    rejected_files: list[str]
    warnings: list[str]


def _normalise_columns(cols: Iterable[str]) -> list[str]:
    normalised: list[str] = []
    for c in cols:
        c = c.strip().lower()
        c = c.replace(" ", "_").replace("-", "_")
        c = re.sub(r"[^a-z0-9_]", "_", c)
        c = re.sub(r"_+", "_", c).strip("_")
        normalised.append(c)
    return normalised


def _sniff_dialect(path: Path) -> csv.Dialect:
    sample = path.read_text(encoding="utf-8", errors="replace")[:4096]
    try:
        return csv.Sniffer().sniff(sample)
    except csv.Error:

        class _Default(csv.Dialect):
            delimiter = ","
            quotechar = '"'
            doublequote = True
            skipinitialspace = True
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL

        return _Default()


def _get_hash_salt() -> str:
    salt = os.getenv("PHISHVIZ_HASH_SALT", "").strip()
    if salt and len(salt) >= 16:
        return salt

    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "PHISHVIZ_HASH_SALT":
                candidate = v.strip().strip('"').strip("'")
                if len(candidate) >= 16:
                    return candidate

    raise RuntimeError(
        "PHISHVIZ_HASH_SALT is missing or too short. "
        "Set a strong secret salt (16+ chars) before running ingestion."
    )


def _hash_value(value: str, *, salt: str) -> str:
    data = (salt + value).encode("utf-8", errors="ignore")
    return hashlib.sha256(data).hexdigest()


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in _MISSING_TOKENS


def _parse_results_timestamps(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(
        values, format=_AST_RESULTS_TS_FORMAT, errors="coerce", utc=False
    )
    if parsed.isna().any():
        missing_mask = parsed.isna()
        parsed2 = pd.to_datetime(values[missing_mask], errors="coerce", utc=False)
        parsed.loc[missing_mask] = parsed2
    return parsed


def _detect_results_variant(cols: set[str]) -> str:
    if any(c.startswith("attachmentopened_") for c in cols):
        return "malware_attachment"
    if any(c.startswith("emaillinkclicked_") for c in cols) or any(
        c.startswith("credsupplied_") for c in cols
    ):
        return "credential_harvest"
    return "unknown"


def _validate_results_export(
    df: pd.DataFrame, *, source_name: str
) -> tuple[bool, list[str], list[str], str]:
    errors: list[str] = []
    warnings: list[str] = []

    cols = set(df.columns)

    prohibited = sorted(
        c
        for c in cols
        if any(p.fullmatch(c) or p.match(c) for p in PROHIBITED_COLUMN_PATTERNS)
    )
    prohibited = [c for c in prohibited if c not in RESULTS_ALLOWLIST_RAW]
    if prohibited:
        errors.append(
            "Prohibited identifier/content columns detected: " + ", ".join(prohibited)
        )

    variant = _detect_results_variant(cols)
    if variant == "unknown":
        errors.append("Unsupported Results export variant (technique not recognised).")

    missing_common = sorted(RESULTS_REQUIRED_COMMON - cols)
    if missing_common:
        errors.append("Missing required common columns: " + ", ".join(missing_common))

    if variant in RESULTS_REQUIRED_BY_VARIANT:
        missing_variant = sorted(RESULTS_REQUIRED_BY_VARIANT[variant] - cols)
        if missing_variant:
            errors.append(
                f"Missing required {variant} columns: " + ", ".join(missing_variant)
            )

    if not (("usermail" in cols) or ("username" in cols) or ("subject_id" in cols)):
        errors.append(
            "Missing subject identifier: expected UserMail or UserName (or pre-built subject_id)."
        )

    unknown = sorted(cols - RESULTS_ALLOWLIST_RAW - {"source_file"})
    if unknown:
        errors.append(
            "Unknown columns present (schema drift or different export type): "
            + ", ".join(unknown)
        )

    if errors:
        return False, errors, warnings, variant

    # Domain checks
    compromised_values = {str(v).strip().lower() for v in df["compromised"].unique()}
    allowed_compromised = {"yes", "no", *_MISSING_TOKENS}
    bad_comp = sorted(v for v in compromised_values if v not in allowed_compromised)
    if bad_comp:
        warnings.append(
            "Unexpected values in 'compromised': " + ", ".join(bad_comp[:5])
        )

    allowed_last_activity = {
        "messageread",
        "failedtodeliveremail",
        "positivereinforcementmessagedelivered",
        "successfullydeliveredemail",
        "messagedeleted",
        "messagereplied",
        "messageforwarded",
        "outofoffice",
        "reportedemail",
        # Variant specific:
        "emaillinkclicked",
        "credsupplied",
        "attachmentopened",
    }
    last_vals = {
        str(v).strip().lower()
        for v in df["last_simulation_activity"].unique()
        if not _is_missing(v)
    }
    bad_last = sorted(v for v in last_vals if v not in allowed_last_activity)
    if bad_last:
        warnings.append(
            "Unexpected values in 'last_simulation_activity': "
            + ", ".join(bad_last[:5])
        )

    allowed_training_status = {"completed", "not started", "in progress"}
    ts_vals = {
        str(v).strip().lower()
        for v in df["training_status"].unique()
        if not _is_missing(v)
    }
    bad_ts = sorted(v for v in ts_vals if v not in allowed_training_status)
    if bad_ts:
        warnings.append(
            "Unexpected values in 'training_status': " + ", ".join(bad_ts[:5])
        )

    for col in ("outofofficedays", "assigned_trainings", "completed_trainings"):
        bad = 0
        for v in df[col].tolist():
            if _is_missing(v):
                continue
            if not re.fullmatch(r"\d+", str(v).strip()):
                bad += 1
        if bad:
            warnings.append(f"{col} contains {bad} non-integer value(s)")

    # Timestamp parse checks (only for columns present)
    for col in RESULTS_TIMESTAMP_COLUMNS:
        if col not in df.columns:
            continue
        present = df[col][~df[col].map(_is_missing)].astype(str)
        if present.empty:
            continue
        parsed = _parse_results_timestamps(present)
        invalid = present[parsed.isna()]
        if not invalid.empty:
            examples = ", ".join(sorted(set(invalid.tolist()))[:3])
            warnings.append(
                f"{col} has {len(invalid)} unparsable value(s). Example(s): {examples}"
            )

    # Cross-field consistency
    compromised_yes = df["compromised"].astype(str).str.strip().str.lower() == "yes"

    if variant == "credential_harvest":
        missing_comp_ts = df["credsupplied_timestamp_compromised"].map(_is_missing)
        bad = int((compromised_yes & missing_comp_ts).sum())
        if bad:
            warnings.append(
                f"{bad} row(s) compromised=Yes but missing credsupplied timestamp"
            )

        click_present = ~df["emaillinkclicked_timestamp"].map(_is_missing)
        for dep in ("emaillinkclicked_browser", "emaillinkclicked_clicksource"):
            if dep in df.columns:
                dep_missing = df[dep].map(_is_missing)
                bad = int((click_present & dep_missing).sum())
                if bad:
                    warnings.append(
                        f"{bad} row(s) have a click timestamp but missing {dep}"
                    )

    if variant == "malware_attachment":
        missing_comp_ts = df["attachmentopened_timestamp_compromised"].map(_is_missing)
        bad = int((compromised_yes & missing_comp_ts).sum())
        if bad:
            warnings.append(
                f"{bad} row(s) compromised=Yes but missing attachment opened timestamp"
            )

        attach_present = ~df["attachmentopened_timestamp_compromised"].map(_is_missing)
        for dep in ("attachmentopened_browser", "attachmentopened_clicksource"):
            if dep in df.columns:
                dep_missing = df[dep].map(_is_missing)
                bad = int((attach_present & dep_missing).sum())
                if bad:
                    warnings.append(
                        f"{bad} row(s) have an attachment opened timestamp but missing {dep}"
                    )

    # Delivery timestamps sanity
    has_success = ~df["successfullydeliveredemail_timestamp"].map(_is_missing)
    has_failed = ~df["failedtodeliveremail_timestamp"].map(_is_missing)
    bad = int((~has_success & ~has_failed).sum())
    if bad:
        warnings.append(f"{bad} row(s) missing both delivery timestamps")

    return True, errors, warnings, variant


def _pseudonymise_subject(
    df: pd.DataFrame, *, salt: str
) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    has_mail = "usermail" in df.columns
    has_name = "username" in df.columns
    if not (has_mail or has_name):
        return df, warnings

    mail_series = (
        df["usermail"].fillna("").astype(str) if has_mail else pd.Series([""] * len(df))
    )
    name_series = (
        df["username"].fillna("").astype(str) if has_name else pd.Series([""] * len(df))
    )

    def _subject_key(m: str, n: str) -> str:
        m = m.strip()
        n = n.strip()
        if _is_missing(m):
            m = ""
        if _is_missing(n):
            n = ""
        m = m.lower()
        return m if m else n

    keys = [
        _subject_key(m, n) for m, n in zip(mail_series.tolist(), name_series.tolist())
    ]
    df["subject_id"] = [_hash_value(k, salt=salt) if k else "" for k in keys]

    drop_cols: list[str] = []
    if has_mail:
        drop_cols.append("usermail")
    if has_name:
        drop_cols.append("username")

    df = df.drop(columns=drop_cols)
    warnings.append(
        f"Pseudonymised subject fields into subject_id and dropped: {', '.join(drop_cols)}"
    )
    return df, warnings


def _classify_device(value: str) -> tuple[str, str]:
    """
    Coarse device and OS classification from UA-like strings.
    Returns: (device_family, os_family)
    """
    v = (value or "").strip()
    vl = v.lower()

    # Common mobile/tablet patterns
    if "ipad" in vl:
        return "tablet", "ios"
    if "iphone" in vl or "ios/" in vl:
        return "mobile", "ios"
    if "android" in vl:
        return "mobile", "android"

    # Desktop patterns
    if "windows nt" in vl:
        return "desktop", "windows"
    if "macintosh" in vl or "mac os x" in vl:
        return "desktop", "macos"
    if "linux" in vl:
        return "desktop", "linux"

    # Office / client app patterns
    if "ms-office" in vl or "msoffice" in vl or "microsoft office" in vl:
        return "office_app", "windows"

    # Sometimes the device field is just a version like "16.0.19231"
    if re.fullmatch(r"\d+(\.\d+)+", v):
        return "office_app", "unknown"

    return "unknown", "unknown"


def _pseudonymise_patterned_columns(
    df: pd.DataFrame, *, salt: str
) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []

    # IPs: hash then drop raw
    ip_cols = [c for c in df.columns if c.endswith("_ip")]
    for col in ip_cols:
        df[f"{col}_id"] = (
            df[col]
            .fillna("")
            .astype(str)
            .map(
                lambda v: (
                    _hash_value(v.strip(), salt=salt)
                    if v.strip() and not _is_missing(v)
                    else ""
                )
            )
        )
        df = df.drop(columns=[col])
        warnings.append(f"Pseudonymised and dropped IP column: {col} -> {col}_id")

    # Devices: derive coarse families, hash then drop raw
    device_cols = [c for c in df.columns if c.endswith("_device")]
    for col in device_cols:
        series = df[col].fillna("").astype(str)

        device_family_col = col.replace("_device", "_device_family")
        os_family_col = col.replace("_device", "_os_family")

        families = series.map(
            lambda v: _classify_device(v)[0] if not _is_missing(v) else ""
        )
        os_families = series.map(
            lambda v: _classify_device(v)[1] if not _is_missing(v) else ""
        )

        df[device_family_col] = families
        df[os_family_col] = os_families

        df[f"{col}_id"] = series.map(
            lambda v: (
                _hash_value(v.strip(), salt=salt)
                if v.strip() and not _is_missing(v)
                else ""
            )
        )

        df = df.drop(columns=[col])
        warnings.append(f"Pseudonymised and dropped device column: {col} -> {col}_id")

    return df, warnings


def load_ast_exports(paths: list[str], *, max_mb: int = DEFAULT_MAX_MB) -> IngestResult:
    salt = _get_hash_salt()

    accepted: list[str] = []
    rejected: list[str] = []
    warnings: list[str] = []
    frames: list[pd.DataFrame] = []

    for p in paths:
        path = Path(p).expanduser().resolve()

        if not path.exists() or not path.is_file():
            msg = f"Rejected (not a file): {path}"
            logger.warning(msg)
            rejected.append(str(path))
            warnings.append(msg)
            continue

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            msg = f"Rejected (file too large: {size_mb:.2f} MB > {max_mb} MB): {path}"
            logger.warning(msg)
            rejected.append(str(path))
            warnings.append(msg)
            continue

        dialect = _sniff_dialect(path)

        try:
            df = pd.read_csv(
                path,
                delimiter=dialect.delimiter,
                encoding="utf-8",
                encoding_errors="replace",
                dtype=str,
                keep_default_na=False,
            )
        except Exception as e:
            msg = f"Rejected (read failed): {path} ({type(e).__name__})"
            logger.exception(msg)
            rejected.append(str(path))
            warnings.append(msg)
            continue

        if df.empty:
            msg = f"Rejected (empty CSV): {path}"
            logger.warning(msg)
            rejected.append(str(path))
            warnings.append(msg)
            continue

        df.columns = _normalise_columns(df.columns)

        if df.columns.duplicated().any():
            dupes = sorted(set(df.columns[df.columns.duplicated()].tolist()))
            msg = (
                f"Rejected (duplicate columns after normalisation): {path.name} | "
                + ", ".join(dupes)
            )
            logger.warning(msg)
            rejected.append(str(path))
            warnings.append(msg)
            continue

        ok, errs, warns, variant = _validate_results_export(df, source_name=path.name)

        if not ok:
            msg = f"Rejected (validation failed): {path.name} | " + " | ".join(errs)
            logger.warning(msg)
            rejected.append(str(path))
            warnings.append(msg)
            continue

        # Record variant for downstream FR3 normalisation
        df["results_variant"] = variant
        warnings.append(f"{path.name}: Detected Results variant: {variant}")
        warnings.extend([f"{path.name}: {w}" for w in warns])

        # FR1 pseudonymisation
        df, w1 = _pseudonymise_subject(df, salt=salt)
        df, w2 = _pseudonymise_patterned_columns(df, salt=salt)
        warnings.extend([f"{path.name}: {w}" for w in (w1 + w2)])

        df["source_file"] = path.name

        frames.append(df)
        accepted.append(str(path))

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return IngestResult(
        df=combined, accepted_files=accepted, rejected_files=rejected, warnings=warnings
    )
