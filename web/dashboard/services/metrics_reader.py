from __future__ import annotations

from pathlib import Path

import pandas as pd

_MONTH_MAP = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}


def _read_csv_if_exists(path: Path):
    if path.exists() and path.is_file():
        return pd.read_csv(path)
    return None


def _find_column(df: pd.DataFrame, candidates: list[str]):
    lowered = {col.lower(): col for col in df.columns}
    for candidate in candidates:
        key = candidate.lower()
        if key in lowered:
            return lowered[key]
    return None


def _sum_column(df: pd.DataFrame | None, candidates: list[str]):
    if df is None or df.empty:
        return None
    col = _find_column(df, candidates)
    if not col:
        return None
    return int(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())


def _mean_column(df: pd.DataFrame | None, candidates: list[str]):
    if df is None or df.empty:
        return None
    col = _find_column(df, candidates)
    if not col:
        return None
    value = pd.to_numeric(df[col], errors="coerce").dropna()
    if value.empty:
        return None
    return round(float(value.mean()), 2)


def _format_value(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _build_period_column(df: pd.DataFrame | None):
    if df is None or df.empty:
        return df

    df = df.copy()

    if "campaign_year_month" in df.columns:
        series = df["campaign_year_month"].astype(str).str.strip()

        # Keep ALL rows and already-normalised YYYY-MM values as they are
        df["_dashboard_period"] = series
        return df

    year_col = _find_column(df, ["campaign_year_code", "campaign_year"])
    month_col = _find_column(df, ["campaign_month"])

    if year_col and month_col:
        year_series = (
            df[year_col].astype(str).str.extract(r"(\d{4})", expand=False).fillna("")
        )
        month_series = (
            df[month_col].astype(str).str.strip().str.lower().map(_MONTH_MAP).fillna("")
        )

        df["_dashboard_period"] = year_series + "-" + month_series
    else:
        df["_dashboard_period"] = ""

    return df


def _apply_filters(
    df: pd.DataFrame | None,
    *,
    technique: str = "",
    period: str = "",
):
    if df is None or df.empty:
        return df

    filtered = _build_period_column(df)

    technique_col = _find_column(filtered, ["technique"])
    if technique and technique_col:
        filtered = filtered[
            filtered[technique_col].astype(str).str.lower() == technique.lower()
        ]

    if period and "_dashboard_period" in filtered.columns:
        filtered = filtered[
            filtered["_dashboard_period"].astype(str).str.lower() == period.lower()
        ]

    return filtered


def _table_html(df: pd.DataFrame | None):
    if df is None or df.empty:
        return None

    render_df = df.copy()
    if "_dashboard_period" in render_df.columns:
        render_df = render_df.drop(columns=["_dashboard_period"])

    return render_df.fillna("").to_html(
        index=False,
        border=0,
        classes="metrics-table",
    )


def load_dashboard_data(
    out_dir: Path,
    *,
    selected_technique: str = "",
    selected_period: str = "",
):
    fr4_campaign = _read_csv_if_exists(out_dir / "fr4_campaign_metrics.csv")
    fr5_comparison = _read_csv_if_exists(out_dir / "fr5_cohort_comparison_metrics.csv")
    fr5_trend = _read_csv_if_exists(out_dir / "fr5_cohort_trend_metrics.csv")

    fr4_campaign = _build_period_column(fr4_campaign)
    fr5_comparison = _build_period_column(fr5_comparison)
    fr5_trend = _build_period_column(fr5_trend)

    technique_options = []
    if fr4_campaign is not None and not fr4_campaign.empty:
        technique_col = _find_column(fr4_campaign, ["technique"])
        if technique_col:
            technique_options = sorted(
                {
                    str(v).strip()
                    for v in fr4_campaign[technique_col].dropna().tolist()
                    if str(v).strip()
                }
            )

    period_options = []
    if (
        fr4_campaign is not None
        and not fr4_campaign.empty
        and "_dashboard_period" in fr4_campaign.columns
    ):
        period_options = sorted(
            {
                str(v).strip()
                for v in fr4_campaign["_dashboard_period"].dropna().tolist()
                if str(v).strip()
            }
        )

    fr4_filtered = _apply_filters(
        fr4_campaign,
        technique=selected_technique,
        period=selected_period,
    )
    fr5_comparison_filtered = _apply_filters(
        fr5_comparison,
        technique=selected_technique,
        period=selected_period,
    )
    fr5_trend_filtered = _apply_filters(
        fr5_trend,
        technique=selected_technique,
        period=selected_period,
    )

    summary_cards = [
        {
            "label": "Campaign rows",
            "value": _format_value(0 if fr4_filtered is None else len(fr4_filtered)),
        },
        {
            "label": "Total delivered",
            "value": _format_value(
                _sum_column(fr4_filtered, ["delivered", "n_delivered"])
            ),
        },
        {
            "label": "Mean click rate",
            "value": _format_value(
                _mean_column(fr4_filtered, ["click_rate_pct", "interaction_rate"])
            ),
        },
        {
            "label": "Mean report rate",
            "value": _format_value(
                _mean_column(fr4_filtered, ["report_rate_pct", "report_rate"])
            ),
        },
    ]

    return {
        "summary_cards": summary_cards,
        "campaign_table_html": _table_html(fr4_filtered),
        "cohort_comparison_html": _table_html(fr5_comparison_filtered),
        "cohort_trend_html": _table_html(fr5_trend_filtered),
        "technique_options": technique_options,
        "period_options": period_options,
        "selected_technique": selected_technique,
        "selected_period": selected_period,
    }
