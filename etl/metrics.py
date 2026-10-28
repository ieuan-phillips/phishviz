from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_MONTH_ORDER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class MetricsResult:
    campaign_metrics: pd.DataFrame
    time_metrics: pd.DataFrame
    repeat_metrics: pd.DataFrame
    warnings: list[str]


def _is_nat(x: Any) -> bool:
    return x is None or (isinstance(x, float) and pd.isna(x)) or pd.isna(x)


def _parse_campaign_metadata(source_file: str) -> dict[str, str]:
    """
    Best-effort parse from filenames.

    Supported examples:
      - 'P2026 - January - P1 - TheoUpdateYourDetails - Results.csv'
      - 'P2023 - November - GeorgeClooneySanta - Results.csv'  (no P<number>)
      - '<anything>.csv' (fallback)

    Returns stable keys; unknown fields are empty strings.
    """
    stem = Path(source_file).name
    stem = stem[:-4] if stem.lower().endswith(".csv") else stem

    parts = [p.strip() for p in stem.split(" - ") if p.strip()]

    meta = {
        "campaign_year_code": "",
        "campaign_month": "",
        "campaign_number": "",
        "campaign_label": "",
        "campaign_id": stem,
    }

    # Year code is typically first token like 'P2025'
    if parts and re.fullmatch(r"P\d{4}", parts[0], flags=re.IGNORECASE):
        meta["campaign_year_code"] = parts[0].upper()

    # Month is often second token, keep verbatim
    if len(parts) >= 2:
        meta["campaign_month"] = parts[1]

    # Find campaign number token (P1, P12, N3, etc), but avoid year code (P2025)
    campaign_num = ""
    for p in parts:
        if re.fullmatch(r"[PN]\d{1,3}", p, flags=re.IGNORECASE):
            campaign_num = p.upper()
            break
    meta["campaign_number"] = campaign_num

    # Label heuristics: token after campaign number, else third token
    label = ""
    upper_parts = [x.upper() for x in parts]
    if campaign_num and campaign_num in upper_parts:
        idx = upper_parts.index(campaign_num)
        if idx + 1 < len(parts):
            label = parts[idx + 1]
    else:
        if len(parts) >= 3:
            # e.g. 'P2023 - November - GeorgeClooneySanta - Results'
            label = parts[2]
    meta["campaign_label"] = label

    # Campaign id: prefer 'P2025_P12' when available
    if meta["campaign_year_code"] and meta["campaign_number"]:
        meta["campaign_id"] = f"{meta['campaign_year_code']}_{meta['campaign_number']}"
    else:
        meta["campaign_id"] = stem

    return meta


def _safe_rate(numer: int, denom: int) -> Optional[float]:
    if denom <= 0:
        return None
    return numer / denom


def _timedelta_minutes(event_ts: pd.Series, base_ts: pd.Series) -> pd.Series:
    delta = event_ts - base_ts
    mins = delta.dt.total_seconds() / 60.0
    return mins


def compute_metrics_fr4(df_norm: pd.DataFrame) -> MetricsResult:
    """
    FR4: Behavioural metric computation (aggregate-only outputs).

    Inputs: Normalised Results dataset from FR3.
    Outputs:
      - campaign_metrics: per-campaign (per source_file) rates and counts
      - time_metrics: per-campaign timing distributions for key behaviours
      - repeat_metrics: aggregated repeat behaviour distributions across campaigns
    """
    warnings: list[str] = []

    required = {
        "source_file",
        "subject_id",
        "technique",
        "delivery_status",
        "delivered_ts",
    }
    missing = sorted(required - set(df_norm.columns))
    if missing:
        raise ValueError(
            f"compute_metrics_fr4 missing required columns: {', '.join(missing)}"
        )

    df = df_norm.copy()

    # Add campaign metadata derived from source_file
    meta_rows = df["source_file"].map(_parse_campaign_metadata)
    df["campaign_id"] = meta_rows.map(lambda d: d["campaign_id"])
    df["campaign_label"] = meta_rows.map(lambda d: d["campaign_label"])
    df["campaign_month"] = meta_rows.map(lambda d: d["campaign_month"])
    df["campaign_year_code"] = meta_rows.map(lambda d: d["campaign_year_code"])
    df["campaign_number"] = meta_rows.map(lambda d: d["campaign_number"])

    # Defensive: ensure delivered_ts is datetime
    if not pd.api.types.is_datetime64_any_dtype(df["delivered_ts"]):
        df["delivered_ts"] = pd.to_datetime(df["delivered_ts"], errors="coerce")

    # Deduplicate per campaign+subject to avoid double counting
    # For presence fields, use "any"; for timestamps, take earliest non-null.
    ts_cols = [c for c in df.columns if c.endswith("_ts")]
    presence_cols = ["delivery_status"]

    agg: dict[str, Any] = {
        c: "first" for c in df.columns if c not in ts_cols + presence_cols
    }
    for c in presence_cols:
        agg[c] = "first"

    for c in ts_cols:
        agg[c] = "min"

    df_dedup = (
        df.sort_values("delivered_ts")
        .groupby(["source_file", "subject_id"], as_index=False)
        .agg(agg)
    )

    # Helper masks (delivered population is the main denominator)
    delivered_mask = df_dedup["delivery_status"].astype(str).str.lower() == "delivered"
    failed_mask = df_dedup["delivery_status"].astype(str).str.lower() == "failed"

    # Event presence by technique (columns may be NaT)
    def _present(col: str) -> pd.Series:
        if col not in df_dedup.columns:
            return pd.Series([False] * len(df_dedup), index=df_dedup.index)
        return ~df_dedup[col].isna()

    read_present = _present("message_read_ts")
    report_present = _present("reported_ts")
    click_present = _present("link_clicked_ts")
    cred_present = _present("credential_supplied_ts")
    attach_present = _present("attachment_opened_ts")

    # Compromise indicator
    if "compromised_bool" in df_dedup.columns:
        compromised_present = df_dedup["compromised_bool"].fillna(False).astype(bool)
    else:
        # Fall back: infer from credential supplied or attachment opened
        compromised_present = cred_present | attach_present
        warnings.append(
            "compromised_bool missing, inferred compromise from event timestamps"
        )

    # Campaign metrics (per source_file and technique)
    group_cols = [
        "source_file",
        "campaign_id",
        "campaign_label",
        "campaign_year_code",
        "campaign_month",
        "campaign_number",
        "technique",
    ]

    rows = []
    for keys, g in df_dedup.groupby(group_cols):
        (
            source_file,
            campaign_id,
            campaign_label,
            year_code,
            month,
            number,
            technique,
        ) = keys

        n_total = int(g["subject_id"].nunique())
        n_delivered = int(
            (g["delivery_status"].astype(str).str.lower() == "delivered").sum()
        )
        n_failed = int((g["delivery_status"].astype(str).str.lower() == "failed").sum())

        # Delivered is the denominator for behavioural actions
        denom = n_delivered

        n_read = (
            int(
                (
                    ~g["message_read_ts"].isna()
                    & (g["delivery_status"].astype(str).str.lower() == "delivered")
                ).sum()
            )
            if "message_read_ts" in g.columns
            else 0
        )
        n_reported = (
            int(
                (
                    ~g["reported_ts"].isna()
                    & (g["delivery_status"].astype(str).str.lower() == "delivered")
                ).sum()
            )
            if "reported_ts" in g.columns
            else 0
        )

        if technique == "credential_harvest":
            n_interact = (
                int(
                    (
                        ~g["link_clicked_ts"].isna()
                        & (g["delivery_status"].astype(str).str.lower() == "delivered")
                    ).sum()
                )
                if "link_clicked_ts" in g.columns
                else 0
            )
            n_cred = (
                int(
                    (
                        ~g["credential_supplied_ts"].isna()
                        & (g["delivery_status"].astype(str).str.lower() == "delivered")
                    ).sum()
                )
                if "credential_supplied_ts" in g.columns
                else 0
            )
            n_attach = 0
        elif technique == "malware_attachment":
            n_interact = (
                int(
                    (
                        ~g["attachment_opened_ts"].isna()
                        & (g["delivery_status"].astype(str).str.lower() == "delivered")
                    ).sum()
                )
                if "attachment_opened_ts" in g.columns
                else 0
            )
            n_attach = n_interact
            n_cred = 0
        else:
            n_interact = 0
            n_cred = 0
            n_attach = 0

        n_compromised = int(
            (
                g.get("compromised_bool", pd.Series([False] * len(g)))
                .fillna(False)
                .astype(bool)
                & (g["delivery_status"].astype(str).str.lower() == "delivered")
            ).sum()
        )

        # Training
        n_training_assigned = 0
        n_training_completed = 0
        training_completion_rate = None
        if "assigned_trainings" in g.columns:
            n_training_assigned = int((g["assigned_trainings"].fillna(0) > 0).sum())
        if "training_status_norm" in g.columns:
            n_training_completed = int((g["training_status_norm"] == "completed").sum())
        if n_training_assigned > 0:
            training_completion_rate = n_training_completed / n_training_assigned

        rows.append(
            {
                "source_file": source_file,
                "campaign_id": campaign_id,
                "campaign_label": campaign_label,
                "campaign_year_code": year_code,
                "campaign_month": month,
                "campaign_number": number,
                "technique": technique,
                "n_subjects_total": n_total,
                "n_delivered": n_delivered,
                "n_failed_delivery": n_failed,
                "delivery_rate": _safe_rate(n_delivered, n_total),
                "read_rate": _safe_rate(n_read, denom),
                "interaction_rate": _safe_rate(n_interact, denom),
                "credential_submission_rate": (
                    _safe_rate(n_cred, denom)
                    if technique == "credential_harvest"
                    else None
                ),
                "attachment_open_rate": (
                    _safe_rate(n_attach, denom)
                    if technique == "malware_attachment"
                    else None
                ),
                "compromise_rate": _safe_rate(n_compromised, denom),
                "report_rate": _safe_rate(n_reported, denom),
                "training_assigned_rate": _safe_rate(n_training_assigned, n_total),
                "training_completion_rate": training_completion_rate,
            }
        )

    campaign_metrics = pd.DataFrame(rows)

    campaign_metrics["_campaign_year_num"] = (
        campaign_metrics["campaign_year_code"]
        .astype(str)
        .str.extract(r"(\d{4})", expand=False)
    )
    campaign_metrics["_campaign_year_num"] = pd.to_numeric(
        campaign_metrics["_campaign_year_num"], errors="coerce"
    )

    campaign_metrics["_campaign_month_num"] = (
        campaign_metrics["campaign_month"]
        .astype(str)
        .str.strip()
        .str.lower()
        .map(_MONTH_ORDER)
    )

    campaign_metrics = campaign_metrics.sort_values(
        ["_campaign_year_num", "_campaign_month_num", "campaign_number", "technique"],
        na_position="last",
    ).drop(columns=["_campaign_year_num", "_campaign_month_num"])

    # Time-to metrics (minutes from delivered_ts, delivered only)
    def _time_summary(series_mins: pd.Series) -> dict[str, Any]:
        s = series_mins.dropna()
        if s.empty:
            return {
                "n": 0,
                "mean_mins": None,
                "median_mins": None,
                "p25_mins": None,
                "p75_mins": None,
                "p90_mins": None,
            }
        return {
            "n": int(s.shape[0]),
            "mean_mins": float(s.mean()),
            "median_mins": float(s.median()),
            "p25_mins": float(s.quantile(0.25)),
            "p75_mins": float(s.quantile(0.75)),
            "p90_mins": float(s.quantile(0.90)),
        }

    time_rows = []
    for keys, g in df_dedup.groupby(group_cols):
        (
            source_file,
            campaign_id,
            campaign_label,
            year_code,
            month,
            number,
            technique,
        ) = keys

        g_del = g[g["delivery_status"].astype(str).str.lower() == "delivered"].copy()
        base = g_del["delivered_ts"]

        # Ensure datetime types
        for c in [
            "message_read_ts",
            "reported_ts",
            "link_clicked_ts",
            "credential_supplied_ts",
            "attachment_opened_ts",
        ]:
            if c in g_del.columns and not pd.api.types.is_datetime64_any_dtype(
                g_del[c]
            ):
                g_del[c] = pd.to_datetime(g_del[c], errors="coerce")

        # Common
        if "message_read_ts" in g_del.columns:
            t = _timedelta_minutes(g_del["message_read_ts"], base)
            t[t < 0] = pd.NA
            summ = _time_summary(t)
            time_rows.append(
                {
                    **{
                        "source_file": source_file,
                        "campaign_id": campaign_id,
                        "campaign_label": campaign_label,
                        "campaign_year_code": year_code,
                        "campaign_month": month,
                        "campaign_number": number,
                        "technique": technique,
                        "metric": "time_to_read_mins",
                    },
                    **summ,
                }
            )

        if "reported_ts" in g_del.columns:
            t = _timedelta_minutes(g_del["reported_ts"], base)
            t[t < 0] = pd.NA
            summ = _time_summary(t)
            time_rows.append(
                {
                    **{
                        "source_file": source_file,
                        "campaign_id": campaign_id,
                        "campaign_label": campaign_label,
                        "campaign_year_code": year_code,
                        "campaign_month": month,
                        "campaign_number": number,
                        "technique": technique,
                        "metric": "time_to_report_mins",
                    },
                    **summ,
                }
            )

        # Technique specific
        if technique == "credential_harvest":
            if "link_clicked_ts" in g_del.columns:
                t = _timedelta_minutes(g_del["link_clicked_ts"], base)
                t[t < 0] = pd.NA
                summ = _time_summary(t)
                time_rows.append(
                    {
                        **{
                            "source_file": source_file,
                            "campaign_id": campaign_id,
                            "campaign_label": campaign_label,
                            "campaign_year_code": year_code,
                            "campaign_month": month,
                            "campaign_number": number,
                            "technique": technique,
                            "metric": "time_to_click_mins",
                        },
                        **summ,
                    }
                )

            if "credential_supplied_ts" in g_del.columns:
                t = _timedelta_minutes(g_del["credential_supplied_ts"], base)
                t[t < 0] = pd.NA
                summ = _time_summary(t)
                time_rows.append(
                    {
                        **{
                            "source_file": source_file,
                            "campaign_id": campaign_id,
                            "campaign_label": campaign_label,
                            "campaign_year_code": year_code,
                            "campaign_month": month,
                            "campaign_number": number,
                            "technique": technique,
                            "metric": "time_to_credential_submit_mins",
                        },
                        **summ,
                    }
                )

        if technique == "malware_attachment":
            if "attachment_opened_ts" in g_del.columns:
                t = _timedelta_minutes(g_del["attachment_opened_ts"], base)
                t[t < 0] = pd.NA
                summ = _time_summary(t)
                time_rows.append(
                    {
                        **{
                            "source_file": source_file,
                            "campaign_id": campaign_id,
                            "campaign_label": campaign_label,
                            "campaign_year_code": year_code,
                            "campaign_month": month,
                            "campaign_number": number,
                            "technique": technique,
                            "metric": "time_to_attachment_open_mins",
                        },
                        **summ,
                    }
                )

    time_metrics = pd.DataFrame(time_rows)

    # Repeat behaviour summary (aggregate distribution across campaigns)
    # Count in how many distinct campaigns a subject performed each behaviour.
    df_rep = df_dedup.copy()
    df_rep["campaign_key"] = df_rep["campaign_id"].astype(str)

    df_rep["did_interact"] = False
    df_rep.loc[df_rep["technique"] == "credential_harvest", "did_interact"] = (
        ~df_rep.get("link_clicked_ts", pd.Series([pd.NaT] * len(df_rep))).isna()
    )
    df_rep.loc[df_rep["technique"] == "malware_attachment", "did_interact"] = (
        ~df_rep.get("attachment_opened_ts", pd.Series([pd.NaT] * len(df_rep))).isna()
    )

    df_rep["did_report"] = ~df_rep.get(
        "reported_ts", pd.Series([pd.NaT] * len(df_rep))
    ).isna()
    df_rep["did_compromise"] = (
        df_rep.get("compromised_bool", pd.Series([False] * len(df_rep)))
        .fillna(False)
        .astype(bool)
    )

    per_subject = (
        df_rep.groupby("subject_id")
        .agg(
            campaigns_targeted=("campaign_key", "nunique"),
            campaigns_interacted=("did_interact", "sum"),
            campaigns_reported=("did_report", "sum"),
            campaigns_compromised=("did_compromise", "sum"),
        )
        .reset_index(drop=False)
    )

    # Aggregate-only distributions (safe for dissertation)
    repeat_metrics = (
        per_subject.groupby(
            [
                "campaigns_targeted",
                "campaigns_interacted",
                "campaigns_reported",
                "campaigns_compromised",
            ]
        )
        .size()
        .reset_index(name="n_subjects")
        .sort_values(
            [
                "campaigns_targeted",
                "campaigns_interacted",
                "campaigns_reported",
                "campaigns_compromised",
            ]
        )
    )

    return MetricsResult(
        campaign_metrics=campaign_metrics,
        time_metrics=time_metrics,
        repeat_metrics=repeat_metrics,
        warnings=warnings,
    )


def write_metrics(result: MetricsResult, out_dir: str) -> None:
    """
    Writes aggregate-only metric tables to disk.
    """
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)

    result.campaign_metrics.to_csv(p / "fr4_campaign_metrics.csv", index=False)
    result.time_metrics.to_csv(p / "fr4_time_metrics.csv", index=False)
    result.repeat_metrics.to_csv(p / "fr4_repeat_metrics.csv", index=False)


# ----------------------------
# FR5: Cohort segmentation and comparison (optional)
# ----------------------------


@dataclass(frozen=True)
class CohortMetricsResult:
    cohort_campaign_metrics: pd.DataFrame
    cohort_trend_metrics: pd.DataFrame
    cohort_comparison_metrics: pd.DataFrame
    warnings: list[str]


def load_cohort_config(path: str) -> dict[str, Any]:
    """
    Loads optional cohort configuration (JSON). If the file does not exist,
    returns a disabled config.
    """
    p = Path(path)
    if not p.exists():
        return {
            "enabled": False,
            "method": "hybrid",
            "start_cohort": "A",
            "p_parity_from_year": 2024,
            "unassigned_policy": "exclude",
            "allow_partial_year_chronological": False,
            "exclude_campaign_number_regex": [],
            "all_year_months": [],
            "include_all_in_metrics": True,
            "include_all_in_comparison": False,
            "require_assignment_when_enabled": False,
            "min_campaign_year": None,
            "max_campaign_year": None,
        }

    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("cohort config must be a JSON object")

    # Defaults
    out = {
        "enabled": bool(data.get("enabled", False)),
        "method": str(data.get("method", "auto")).strip().lower(),
        "start_cohort": str(data.get("start_cohort", "A")).strip().upper(),
        "p_parity_from_year": int(data.get("p_parity_from_year", 2024) or 2024),
        "unassigned_policy": str(data.get("unassigned_policy", "exclude"))
        .strip()
        .lower(),
        "allow_partial_year_chronological": bool(
            data.get("allow_partial_year_chronological", False)
        ),
        "exclude_campaign_number_regex": list(
            data.get("exclude_campaign_number_regex", [])
        ),
        "all_year_months": list(data.get("all_year_months", [])),
        "include_all_in_metrics": bool(data.get("include_all_in_metrics", True)),
        "include_all_in_comparison": bool(data.get("include_all_in_comparison", False)),
        "require_assignment_when_enabled": bool(
            data.get("require_assignment_when_enabled", False)
        ),
        "min_campaign_year": data.get("min_campaign_year", None),
        "max_campaign_year": data.get("max_campaign_year", None),
    }
    if out["start_cohort"] not in {"A", "B"}:
        raise ValueError("start_cohort must be 'A' or 'B'")
    if out["method"] not in {
        "auto",
        "hybrid",
        "token",
        "map",
        "p_number_parity",
        "chronological_alternation",
    }:
        raise ValueError(
            "method must be one of: auto, hybrid, token, map, p_number_parity, chronological_alternation"
        )
    return out


def load_cohort_map_csv(path: str) -> dict[str, str]:
    """
    Optional explicit overrides, keyed by source_file basename.
    CSV columns: source_file,cohort
    """
    p = Path(path)
    if not p.exists():
        return {}

    df = pd.read_csv(p, dtype=str).fillna("")
    if "source_file" not in df.columns or "cohort" not in df.columns:
        raise ValueError("cohort_map.csv must contain columns: source_file, cohort")

    out: dict[str, str] = {}
    for _, r in df.iterrows():
        k = str(r["source_file"]).strip()
        v = str(r["cohort"]).strip().upper()
        if not k or not v:
            continue
        if v in {"A&B", "A,B", "ALL"}:
            v = "ALL"
        out[Path(k).name] = v
    return out


_TOKEN_RE = re.compile(r"\[(A|B|ALL|A&B)\]", flags=re.IGNORECASE)
_PART_TOKEN_RE = re.compile(r"^(A|B|ALL|A&B)$", flags=re.IGNORECASE)
_PNUM_RE = re.compile(r"\bP(\d{1,3})\b", flags=re.IGNORECASE)


def _cohort_from_tokens(source_file: str) -> Optional[str]:
    name = Path(source_file).name
    m = _TOKEN_RE.search(name)
    if m:
        v = m.group(1).upper()
        return "ALL" if v in {"ALL", "A&B"} else v

    # Also accept tokens as standalone parts separated by " - "
    parts = [p.strip() for p in name.replace(".csv", "").split(" - ")]
    for p in parts:
        if _PART_TOKEN_RE.fullmatch(p):
            v = p.upper()
            return "ALL" if v in {"ALL", "A&B"} else v
    return None


def _campaign_number_from_filename(source_file: str) -> Optional[int]:
    """
    Extract P<number> where number is <= 3 digits (avoids P2025 year prefix).
    """
    name = Path(source_file).name
    m = _PNUM_RE.search(name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _year_month_key(ts: Any) -> str:
    if ts is None or pd.isna(ts):
        return ""
    dt = pd.to_datetime(ts, errors="coerce")
    if pd.isna(dt):
        return ""
    return f"{dt.year:04d}-{dt.month:02d}"


def _flip_ab(v: str) -> str:
    return "B" if v == "A" else "A"


def _matches_any_regex(value: str, patterns: list[str]) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, value):
                return True
        except re.error:
            # Ignore bad patterns but warn upstream
            continue
    return False


def _assign_cohorts(
    df_dedup: pd.DataFrame,
    *,
    cohort_config: dict[str, Any],
    cohort_map: dict[str, str],
) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []

    df = df_dedup.copy()

    # Campaign start is used for ordering and year/month inference
    if "delivered_ts" in df.columns and pd.api.types.is_datetime64_any_dtype(
        df["delivered_ts"]
    ):
        pass
    else:
        df["delivered_ts"] = pd.to_datetime(df.get("delivered_ts"), errors="coerce")

    campaign_start = (
        df.groupby("source_file", as_index=False)["delivered_ts"]
        .min()
        .rename(columns={"delivered_ts": "campaign_start_ts"})
    )
    df = df.merge(campaign_start, on="source_file", how="left")

    # Derive campaign metadata from filename to support filtering
    meta_rows = df["source_file"].map(_parse_campaign_metadata)
    df["campaign_number_token"] = meta_rows.map(lambda d: d.get("campaign_number", ""))
    df["campaign_year_code"] = meta_rows.map(lambda d: d.get("campaign_year_code", ""))

    # Base cohort via explicit map or tokens
    def _base_cohort(sf: str) -> str:
        base = Path(sf).name
        if base in cohort_map:
            return cohort_map[base]
        tok = _cohort_from_tokens(base)
        return tok or ""

    df["cohort"] = df["source_file"].map(_base_cohort)

    # Determine ALL campaigns based on configured year-month keys
    all_ym = {
        str(x).strip()
        for x in cohort_config.get("all_year_months", [])
        if str(x).strip()
    }
    if all_ym:
        ym_series = df["campaign_start_ts"].map(_year_month_key)
        all_mask = ym_series.isin(all_ym)
        df.loc[all_mask, "cohort"] = "ALL"

    # Exclusions (eg N campaigns) should not influence alternation
    excl_patterns = list(cohort_config.get("exclude_campaign_number_regex", []))
    if excl_patterns:
        excl_mask = (
            df["campaign_number_token"]
            .astype(str)
            .map(lambda s: _matches_any_regex(s, excl_patterns))
        )
    else:
        excl_mask = pd.Series([False] * len(df), index=df.index)
    df["cohort_excluded"] = excl_mask

    # Hybrid inference: P-number parity (where available) then chronological alternation fallback
    method = str(cohort_config.get("method", "hybrid")).strip().lower()
    if method == "auto":
        method = "hybrid"  # alias

    allow_parity = method in {"hybrid", "p_number_parity"}
    allow_chrono = method in {"hybrid", "chronological_alternation"}

    if allow_parity or allow_chrono:
        # Work at campaign granularity
        camp = (
            df[
                [
                    "source_file",
                    "campaign_start_ts",
                    "campaign_number_token",
                    "campaign_year_code",
                    "cohort",
                    "cohort_excluded",
                ]
            ]
            .drop_duplicates(subset=["source_file"])
            .copy()
        )

        # Infer year from campaign_start_ts; fall back to filename year code if missing
        camp["campaign_start_ts"] = pd.to_datetime(
            camp["campaign_start_ts"], errors="coerce"
        )
        camp["campaign_year"] = camp["campaign_start_ts"].dt.year

        def _year_from_code(v: str) -> Optional[int]:
            m = re.fullmatch(r"P(\d{4})", str(v).strip(), flags=re.IGNORECASE)
            return int(m.group(1)) if m else None

        missing_year = camp["campaign_year"].isna()
        if missing_year.any():
            camp.loc[missing_year, "campaign_year"] = camp.loc[
                missing_year, "campaign_year_code"
            ].map(_year_from_code)

        # Apply min/max year filters
        min_y = cohort_config.get("min_campaign_year", None)
        max_y = cohort_config.get("max_campaign_year", None)
        if min_y is not None:
            camp = camp[camp["campaign_year"].fillna(0) >= int(min_y)]
        if max_y is not None:
            camp = camp[camp["campaign_year"].fillna(9999) <= int(max_y)]

        # Sort by timestamp; keep deterministic fallback on filename
        camp = camp.sort_values(
            by=["campaign_year", "campaign_start_ts", "source_file"],
            ascending=[True, True, True],
            na_position="last",
        )

        # Always mark excluded campaigns for removal from FR5 outputs
        camp.loc[camp["cohort_excluded"].astype(bool), "cohort"] = "EXCLUDED"

        # Parity assignment (P<number>) for 2024+ exports (configurable)
        if allow_parity:
            p_from = int(cohort_config.get("p_parity_from_year", 2024) or 2024)
            start = str(cohort_config.get("start_cohort", "A")).upper()

            def _parity(p_num: int) -> str:
                return start if (p_num % 2) == 1 else _flip_ab(start)

            camp["p_num"] = camp["source_file"].map(_campaign_number_from_filename)
            mask = (
                (camp["cohort"].astype(str).str.strip() == "")
                & (~camp["cohort_excluded"].astype(bool))
                & (camp["p_num"].notna())
                & (camp["campaign_year"].fillna(0) >= p_from)
            )
            if mask.any():
                camp.loc[mask, "cohort"] = (
                    camp.loc[mask, "p_num"].astype(int).map(_parity)
                )

        # Chronological alternation fallback for anything still unassigned
        assigned: dict[str, str] = {}
        if allow_chrono:
            start = str(cohort_config.get("start_cohort", "A")).upper()
            strict = method == "chronological_alternation"
            allow_partial = bool(
                cohort_config.get("allow_partial_year_chronological", False)
            )

            for year, g in camp.groupby("campaign_year", dropna=False):
                current = start

                # Optional warning when relying on chronological fallback without a clear year start
                if strict and (not allow_partial) and pd.notna(year):
                    blanks = g[
                        (g["cohort"].astype(str).str.strip() == "")
                        & (~g["cohort_excluded"].astype(bool))
                    ]
                    if not blanks.empty:
                        jan_present = False
                        if g["campaign_start_ts"].notna().any():
                            jan_present = bool(
                                (g["campaign_start_ts"].dt.month == 1).any()
                            )
                        if not jan_present:
                            warnings.append(
                                f"Chronological cohort inference used for year {int(year)} but no January campaign is present. "
                                "If cohorts look offset, include earlier exports or add explicit overrides/tokens."
                            )

                for _, r in g.iterrows():
                    sf = r["source_file"]
                    already = str(r.get("cohort", "")).strip().upper()
                    is_excluded = bool(r.get("cohort_excluded", False))

                    if is_excluded or already == "EXCLUDED":
                        assigned[sf] = "EXCLUDED"
                        continue

                    if already == "ALL":
                        assigned[sf] = "ALL"
                        # ALL does not consume an alternation slot
                        continue

                    if already in {"A", "B"}:
                        if strict and already != current:
                            warnings.append(
                                f"Cohort sequence mismatch in {int(year) if pd.notna(year) else 'unknown year'}: "
                                f"expected {current} but {sf} is labelled {already}"
                            )
                        assigned[sf] = already
                        current = _flip_ab(already)
                        continue

                    if already == "":
                        assigned[sf] = current
                        current = _flip_ab(current)
                        continue

                    assigned[sf] = already

        # Merge back: prefer assigned (chrono) where present, else use campaign-level cohort
        camp_map = {
            str(r.source_file): str(r.cohort).strip().upper()
            for r in camp.itertuples(index=False)
        }

        def _final(sf: str) -> str:
            if sf in assigned:
                return assigned[sf]
            return camp_map.get(sf, "")

        df["cohort"] = df["source_file"].map(_final)
    # Final checks
    if bool(cohort_config.get("enabled", False)) and bool(
        cohort_config.get("require_assignment_when_enabled", False)
    ):
        unassigned = sorted(
            {
                Path(x).name
                for x in df.loc[
                    df["cohort"].astype(str).str.strip() == "", "source_file"
                ].unique()
            }
        )
        if unassigned:
            warnings.append(
                "Cohort segmentation enabled but some exports remain unassigned: "
                + ", ".join(unassigned[:10])
                + ("..." if len(unassigned) > 10 else "")
            )

    # Tidy canonical cohort values
    def _canon(v: Any) -> str:
        s = str(v).strip().upper()
        if s in {"A&B", "A,B", "ALL"}:
            return "ALL"
        if s == "":
            return "UNASSIGNED"
        return s

    df["cohort"] = df["cohort"].map(_canon)

    return df, warnings


def _build_fr5_cohort_comparison(
    cohort_trend_metrics: pd.DataFrame,
    cohort_config: dict[str, Any],
) -> pd.DataFrame:
    """
    Build FR5 cohort comparison table (A vs B) with delta columns.

    Rows produced:
      1) Per (campaign_year_month, technique)
      2) Per (ALL months, technique)  <- new roll-up rows
      3) (ALL months, ALL techniques) <- existing overall row

    Delta columns are only non-null when both A and B exist for the same row key.
    """
    if cohort_trend_metrics.empty:
        return pd.DataFrame()

    include_all = bool(cohort_config.get("include_all_in_comparison", False))

    compare_df = cohort_trend_metrics.copy()
    if not include_all:
        compare_df = compare_df[compare_df["cohort"].isin(["A", "B"])].copy()

    if compare_df.empty:
        return pd.DataFrame()

    numeric_cols = [
        "n_subjects",
        "n_delivered",
        "n_read",
        "n_interacted",
        "n_reported",
        "n_compromised",
    ]

    # Roll-up 1: ALL months per technique (counts summed, rates recomputed)
    by_tech = compare_df.groupby(["cohort", "technique"], as_index=False)[
        numeric_cols
    ].sum()
    by_tech["campaign_year_month"] = "ALL"
    by_tech["read_rate"] = by_tech.apply(
        lambda r: _safe_rate(int(r["n_read"]), int(r["n_delivered"])), axis=1
    )
    by_tech["interact_rate"] = by_tech.apply(
        lambda r: _safe_rate(int(r["n_interacted"]), int(r["n_delivered"])), axis=1
    )
    by_tech["report_rate"] = by_tech.apply(
        lambda r: _safe_rate(int(r["n_reported"]), int(r["n_delivered"])), axis=1
    )
    by_tech["compromise_rate"] = by_tech.apply(
        lambda r: _safe_rate(int(r["n_compromised"]), int(r["n_delivered"])), axis=1
    )

    # Roll-up 2: ALL months, ALL techniques (counts summed, rates recomputed)
    overall = compare_df.groupby(["cohort"], as_index=False)[numeric_cols].sum()
    overall["campaign_year_month"] = "ALL"
    overall["technique"] = "ALL"
    overall["read_rate"] = overall.apply(
        lambda r: _safe_rate(int(r["n_read"]), int(r["n_delivered"])), axis=1
    )
    overall["interact_rate"] = overall.apply(
        lambda r: _safe_rate(int(r["n_interacted"]), int(r["n_delivered"])), axis=1
    )
    overall["report_rate"] = overall.apply(
        lambda r: _safe_rate(int(r["n_reported"]), int(r["n_delivered"])), axis=1
    )
    overall["compromise_rate"] = overall.apply(
        lambda r: _safe_rate(int(r["n_compromised"]), int(r["n_delivered"])), axis=1
    )

    # Combine original + roll-ups
    compare_plus = pd.concat([compare_df, by_tech, overall], ignore_index=True)

    pivot = compare_plus.pivot_table(
        index=["campaign_year_month", "technique"],
        columns="cohort",
        values=["read_rate", "interact_rate", "report_rate", "compromise_rate"],
        aggfunc="first",
    )
    pivot.columns = ["_".join([a, b]) for a, b in pivot.columns.to_flat_index()]
    pivot = pivot.reset_index()

    # Deltas (A - B) where both exist on the row
    for metric in ["read_rate", "interact_rate", "report_rate", "compromise_rate"]:
        a = f"{metric}_A"
        b = f"{metric}_B"
        if a in pivot.columns and b in pivot.columns:
            pivot[f"{metric}_delta_A_minus_B"] = pivot[a] - pivot[b]

    # Sort so ALL-per-technique rows appear above ALL/ALL
    def _sort_key(row: pd.Series) -> tuple:
        ym = row["campaign_year_month"]
        tech = row["technique"]
        if ym == "ALL" and tech != "ALL":
            return (9998, tech)
        if ym == "ALL" and tech == "ALL":
            return (9999, tech)
        return (0, f"{ym}_{tech}")

    pivot["_k"] = pivot.apply(_sort_key, axis=1)
    pivot = pivot.sort_values("_k").drop(columns=["_k"])

    return pivot


def compute_metrics_fr5(
    df_norm: pd.DataFrame,
    *,
    cohort_config: dict[str, Any],
    cohort_map: dict[str, str],
) -> CohortMetricsResult:
    """
    FR5: Cohort segmentation and comparison (optional).

    - Adds a 'cohort' label at campaign level using config-driven inference.
    - Computes cohort-level aggregates and simple A vs B comparisons over time.

    Note: This is campaign-level cohort labelling. An 'ALL' campaign is treated as
    a separate cohort, and can be excluded from A vs B comparisons.
    """
    warnings: list[str] = []

    if not bool(cohort_config.get("enabled", False)):
        return CohortMetricsResult(
            cohort_campaign_metrics=pd.DataFrame(),
            cohort_trend_metrics=pd.DataFrame(),
            cohort_comparison_metrics=pd.DataFrame(),
            warnings=["FR5 cohort segmentation disabled (config enabled=false)."],
        )

    required = {
        "source_file",
        "subject_id",
        "technique",
        "delivery_status",
        "delivered_ts",
    }
    missing = sorted(required - set(df_norm.columns))
    if missing:
        raise ValueError(
            f"compute_metrics_fr5 missing required columns: {', '.join(missing)}"
        )

    # Deduplicate like FR4
    df = df_norm.copy()
    if not pd.api.types.is_datetime64_any_dtype(df["delivered_ts"]):
        df["delivered_ts"] = pd.to_datetime(df["delivered_ts"], errors="coerce")

    ts_cols = [c for c in df.columns if c.endswith("_ts")]
    presence_cols = ["delivery_status"]

    agg: dict[str, Any] = {
        c: "first" for c in df.columns if c not in ts_cols + presence_cols
    }
    for c in presence_cols:
        agg[c] = "first"
    for c in ts_cols:
        agg[c] = "min"

    df_dedup = (
        df.sort_values("delivered_ts")
        .groupby(["source_file", "subject_id"], as_index=False)
        .agg(agg)
    )

    # Attach cohort labels
    df_c, w = _assign_cohorts(
        df_dedup, cohort_config=cohort_config, cohort_map=cohort_map
    )
    warnings.extend(w)

    # Remove excluded campaigns from cohort metrics entirely
    df_c = df_c[df_c["cohort"] != "EXCLUDED"].copy()

    # Optional filters
    unassigned_policy = (
        str(cohort_config.get("unassigned_policy", "exclude")).strip().lower()
    )
    if unassigned_policy == "exclude":
        df_c = df_c[df_c["cohort"] != "UNASSIGNED"].copy()

    if not bool(cohort_config.get("include_all_in_metrics", True)):
        df_c = df_c[df_c["cohort"] != "ALL"].copy()

    # Campaign start / month key
    df_c["campaign_start_ts"] = pd.to_datetime(
        df_c["campaign_start_ts"], errors="coerce"
    )
    df_c["campaign_year_month"] = df_c["campaign_start_ts"].map(_year_month_key)

    # Compute per campaign + cohort metrics (aggregate-only)
    rows = []
    group_cols = ["cohort", "source_file", "campaign_year_month", "technique"]

    for keys, g in df_c.groupby(group_cols):
        cohort, source_file, ym, technique = keys

        n_total = int(g["subject_id"].nunique())
        n_delivered = int(
            (g["delivery_status"].astype(str).str.lower() == "delivered").sum()
        )
        denom = n_delivered

        def _count_present(col: str) -> int:
            if col not in g.columns:
                return 0
            return int(
                (
                    ~g[col].isna()
                    & (g["delivery_status"].astype(str).str.lower() == "delivered")
                ).sum()
            )

        n_read = _count_present("message_read_ts")
        n_reported = _count_present("reported_ts")

        if technique == "credential_harvest":
            n_interact = _count_present("link_clicked_ts")
            n_compromise = _count_present("credential_supplied_ts")
        elif technique == "malware_attachment":
            n_interact = _count_present("attachment_opened_ts")
            n_compromise = n_interact
        else:
            n_interact = 0
            n_compromise = 0

        rows.append(
            {
                "cohort": cohort,
                "source_file": source_file,
                "campaign_year_month": ym,
                "technique": technique,
                "n_subjects": n_total,
                "n_delivered": n_delivered,
                "n_read": n_read,
                "read_rate": _safe_rate(n_read, denom),
                "n_interacted": n_interact,
                "interact_rate": _safe_rate(n_interact, denom),
                "n_reported": n_reported,
                "report_rate": _safe_rate(n_reported, denom),
                "n_compromised": n_compromise,
                "compromise_rate": _safe_rate(n_compromise, denom),
            }
        )

    cohort_campaign_metrics = pd.DataFrame(rows)

    # Trend metrics (aggregate per cohort per month)
    if cohort_campaign_metrics.empty:
        cohort_trend_metrics = pd.DataFrame()
    else:
        numeric_cols = [
            "n_subjects",
            "n_delivered",
            "n_read",
            "n_interacted",
            "n_reported",
            "n_compromised",
        ]
        trend = cohort_campaign_metrics.groupby(
            ["cohort", "campaign_year_month", "technique"], as_index=False
        )[numeric_cols].sum()
        trend["read_rate"] = trend.apply(
            lambda r: _safe_rate(int(r["n_read"]), int(r["n_delivered"])), axis=1
        )
        trend["interact_rate"] = trend.apply(
            lambda r: _safe_rate(int(r["n_interacted"]), int(r["n_delivered"])), axis=1
        )
        trend["report_rate"] = trend.apply(
            lambda r: _safe_rate(int(r["n_reported"]), int(r["n_delivered"])), axis=1
        )
        trend["compromise_rate"] = trend.apply(
            lambda r: _safe_rate(int(r["n_compromised"]), int(r["n_delivered"])), axis=1
        )
        cohort_trend_metrics = trend

    # Cohort comparison: A vs B deltas over time, per technique
    cohort_comparison_metrics = _build_fr5_cohort_comparison(
        cohort_trend_metrics=cohort_trend_metrics,
        cohort_config=cohort_config,
    )

    return CohortMetricsResult(
        cohort_campaign_metrics=cohort_campaign_metrics,
        cohort_trend_metrics=cohort_trend_metrics,
        cohort_comparison_metrics=cohort_comparison_metrics,
        warnings=warnings,
    )


def write_cohort_metrics(result: CohortMetricsResult, out_dir: str) -> None:
    """
    Writes FR5 aggregate-only cohort tables to disk.
    """
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)

    if not result.cohort_campaign_metrics.empty:
        result.cohort_campaign_metrics.to_csv(
            p / "fr5_cohort_campaign_metrics.csv", index=False
        )
    if not result.cohort_trend_metrics.empty:
        result.cohort_trend_metrics.to_csv(
            p / "fr5_cohort_trend_metrics.csv", index=False
        )
    if not result.cohort_comparison_metrics.empty:
        result.cohort_comparison_metrics.to_csv(
            p / "fr5_cohort_comparison_metrics.csv", index=False
        )
