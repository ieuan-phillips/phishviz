from __future__ import annotations

import warnings
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd


_MISSING_TOKENS = {"", "--", "n/a", "na", "null", "none"}
_AST_RESULTS_TS_FORMAT = "%m/%d/%Y %I:%M:%S %p"


@dataclass(frozen=True)
class NormaliseResult:
    df: pd.DataFrame
    warnings: list[str]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _MISSING_TOKENS


def _parse_ast_timestamp(series: pd.Series) -> pd.Series:
    """
    Deterministic parse for AST Results timestamps.
    1) Strict parse with the known AST format
    2) Fallback parse for any remaining non-missing values
    """
    # If already datetime, return as-is
    if pd.api.types.is_datetime64_any_dtype(series):
        return series

    s = series.copy()

    # Normalise to string for parsing, but keep a clean "present" mask
    s_str = s.astype("string").fillna("")
    present_mask = ~s_str.str.strip().str.lower().isin(_MISSING_TOKENS)

    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

    if present_mask.any():
        # Strict parse first
        strict = pd.to_datetime(
            s_str[present_mask],
            format=_AST_RESULTS_TS_FORMAT,
            errors="coerce",
            utc=False,
        )
        out.loc[present_mask] = strict

        # Fallback parse only where strict failed
        still_bad = out.loc[present_mask].isna()
        if still_bad.any():
            idx = out.loc[present_mask].index[still_bad]

            # Try pandas mixed format if supported, else fall back with warnings suppressed
            try:
                parsed2 = pd.to_datetime(
                    s_str.loc[idx], format="mixed", errors="coerce", utc=False
                )
            except (TypeError, ValueError):
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message="Could not infer format, so each element will be parsed individually.*",
                    )
                    parsed2 = pd.to_datetime(s_str.loc[idx], errors="coerce", utc=False)

            out.loc[idx] = parsed2

    return out


def _snake_from_camel(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    v = str(value).strip()
    if _is_missing(v):
        return ""
    v = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", v)
    return v.lower()


def _browser_family(value: Any) -> str:
    # Handle NaN/None safely
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    v = str(value).strip()
    if _is_missing(v) or v.lower() == "nan":
        return ""

    vl = v.lower()

    if vl.startswith("edge"):
        return "edge"
    if vl.startswith("chrome"):
        return "chrome"
    if vl.startswith("safari"):
        return "safari"
    if "internet explorer" in vl or "msie" in vl:
        return "internet_explorer"

    # Client apps / UA-like strings
    if "outlook" in vl:
        return "outlook"
    if "microsoft office word" in vl or "ms-office" in vl or "msoffice" in vl:
        return "office_word"

    return "other"


def _clicksource_norm(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    v = str(value).strip()
    if _is_missing(v) or v.lower() == "nan":
        return ""

    vl = v.lower()

    if "phishingurl" in vl:
        return "link"
    if "qr" in vl:
        return "qr"
    if "attachment" in vl:
        return "attachment"

    return vl


def normalise_results(df: pd.DataFrame) -> NormaliseResult:
    """
    FR3: Normalise validated Results exports (credential harvest + malware attachment)
    into a stable internal schema with consistent column names and types.
    """
    warnings: list[str] = []

    required = {"subject_id", "results_variant", "source_file", "compromised"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(
            f"normalise_results missing required columns: {', '.join(missing)}"
        )

    out = df.copy()

    # ---- Technique
    out["technique"] = out["results_variant"].map(
        lambda v: (
            "credential_harvest"
            if v == "credential_harvest"
            else ("malware_attachment" if v == "malware_attachment" else "unknown")
        )
    )

    # ---- Boolean compromised
    out["compromised_bool"] = (
        out["compromised"]
        .astype(str)
        .str.strip()
        .str.lower()
        .map({"yes": True, "no": False})
    )
    out = out.drop(columns=["compromised"])

    # ---- Training and counts (typed)
    for col in ["outofofficedays", "assigned_trainings", "completed_trainings"]:
        if col in out.columns:
            out[col] = pd.to_numeric(
                out[col].replace({"--": ""}), errors="coerce"
            ).astype("Int64")

    if "training_status" in out.columns:
        out["training_status_norm"] = (
            out["training_status"]
            .astype(str)
            .str.strip()
            .str.lower()
            .replace({"not started": "not_started", "in progress": "in_progress"})
        )
        out = out.drop(columns=["training_status"])

    if "last_simulation_activity" in out.columns:
        out["last_activity_norm"] = (
            out["last_simulation_activity"].astype(str).map(_snake_from_camel)
        )
        out = out.drop(columns=["last_simulation_activity"])

    # ---- Map technique-specific fields into stable event columns
    rename_map = {
        # Delivery + message handling
        "successfullydeliveredemail_timestamp": "delivered_success_ts",
        "failedtodeliveremail_timestamp": "delivered_fail_ts",
        "messageread_timestamp": "message_read_ts",
        "messagedeleted_timestamp": "message_deleted_ts",
        "messagereplied_timestamp": "message_replied_ts",
        "messageforwarded_timestamp": "message_forwarded_ts",
        "phishing_reported_on": "reported_ts",
        # Positive reinforcement / training messages
        "positivereinforcementmessagedelivered_timestamp": "reinforcement_delivered_ts",
        "positivereinforcementmessagefailed_timestamp": "reinforcement_failed_ts",
        "justsimulationmessagedelivered_timestamp": "just_simulation_delivered_ts",
        "justsimulationmessagefailed_timestamp": "just_simulation_failed_ts",
        "trainingassignmentmessagedelivered_timestamp": "training_assignment_delivered_ts",
        "trainingassignmentmessagefailed_timestamp": "training_assignment_failed_ts",
        # Credential harvest events
        "emaillinkclicked_timestamp": "link_clicked_ts",
        "emaillinkclicked_clicksource": "link_clicked_source_raw",
        "emaillinkclicked_browser": "link_clicked_browser_raw",
        "emaillinkclicked_ip_id": "link_clicked_ip_id",
        "emaillinkclicked_device_id": "link_clicked_device_id",
        "emaillinkclicked_device_family": "link_clicked_device_family",
        "emaillinkclicked_os_family": "link_clicked_os_family",
        "credsupplied_timestamp_compromised": "credential_supplied_ts",
        "credsupplied_clicksource": "credential_supplied_source_raw",
        "credsupplied_browser": "credential_supplied_browser_raw",
        "credsupplied_ip_id": "credential_supplied_ip_id",
        "credsupplied_device_id": "credential_supplied_device_id",
        "credsupplied_device_family": "credential_supplied_device_family",
        "credsupplied_os_family": "credential_supplied_os_family",
        # Malware attachment events
        "attachmentopened_timestamp_compromised": "attachment_opened_ts",
        "attachmentopened_clicksource": "attachment_opened_source_raw",
        "attachmentopened_browser": "attachment_opened_browser_raw",
        "attachmentopened_ip_id": "attachment_opened_ip_id",
        "attachmentopened_device_id": "attachment_opened_device_id",
        "attachmentopened_device_family": "attachment_opened_device_family",
        "attachmentopened_os_family": "attachment_opened_os_family",
    }

    existing = {k: v for k, v in rename_map.items() if k in out.columns}
    out = out.rename(columns=existing)

    # ---- Delivery status and unified delivered timestamp
    def _delivery_status(row: pd.Series) -> str:
        if pd.notna(row.get("delivered_success_ts")) and not _is_missing(
            row.get("delivered_success_ts")
        ):
            return "delivered"
        if pd.notna(row.get("delivered_fail_ts")) and not _is_missing(
            row.get("delivered_fail_ts")
        ):
            return "failed"
        return "unknown"

    # Parse delivery timestamps first, then derive delivered_ts
    for ts_col in ["delivered_success_ts", "delivered_fail_ts"]:
        if ts_col in out.columns:
            out[ts_col] = _parse_ast_timestamp(out[ts_col])

    out["delivery_status"] = out.apply(_delivery_status, axis=1)
    out["delivered_ts"] = out["delivered_success_ts"].combine_first(
        out["delivered_fail_ts"]
    )

    # ---- Parse all timestamp columns that exist
    ts_cols = [c for c in out.columns if c.endswith("_ts")]
    for c in ts_cols:
        if not pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = _parse_ast_timestamp(out[c])

    # ---- Normalise browser families and drop raw browser strings
    for raw_col, fam_col in [
        ("link_clicked_browser_raw", "link_clicked_browser_family"),
        ("credential_supplied_browser_raw", "credential_supplied_browser_family"),
        ("attachment_opened_browser_raw", "attachment_opened_browser_family"),
    ]:
        if raw_col in out.columns:
            out[fam_col] = out[raw_col].astype(str).map(_browser_family)
            out = out.drop(columns=[raw_col])

    # ---- Normalise click sources and drop raw click source strings
    for raw_col, norm_col in [
        ("link_clicked_source_raw", "link_clicked_source"),
        ("credential_supplied_source_raw", "credential_supplied_source"),
        ("attachment_opened_source_raw", "attachment_opened_source"),
    ]:
        if raw_col in out.columns:
            out[norm_col] = out[raw_col].astype(str).map(_clicksource_norm)
            out = out.drop(columns=[raw_col])

    # ---- Final tidy: ensure key columns exist even if blank for a technique
    for col in [
        "link_clicked_ts",
        "credential_supplied_ts",
        "attachment_opened_ts",
        "reported_ts",
    ]:
        if col not in out.columns:
            out[col] = pd.NaT

    return NormaliseResult(df=out, warnings=warnings)
