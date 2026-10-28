# etl/visualise.py
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

# Month name normalisation for FR4 (derived from source_file parsing)
_MONTH_MAP = {
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


def _derive_campaign_year_month_fr4(df: pd.DataFrame) -> pd.Series:
    """
    FR4 tables carry (campaign_year_code, campaign_month). FR5 already carries campaign_year_month.
    This function derives 'YYYY-MM' for FR4 rows, best-effort.
    """

    def _row_to_ym(row: pd.Series) -> str:
        year_code = str(row.get("campaign_year_code", "")).strip()
        month_name = str(row.get("campaign_month", "")).strip()

        if re.fullmatch(r"[Pp]\d{4}", year_code) and month_name:
            year = int(year_code[1:])
            month = _MONTH_MAP.get(month_name.lower())
            if month:
                return f"{year:04d}-{month:02d}"

        return ""

    return df.apply(_row_to_ym, axis=1)


def _safe_label_series(primary: pd.Series, fallback: pd.Series) -> pd.Series:
    """
    Use primary labels where present; fall back to another series (e.g., campaign_id).
    """
    p = primary.astype(str)
    mask = p.str.strip().replace({"nan": ""}).str.len() > 0
    out = fallback.astype(str)
    out.loc[mask] = p.loc[mask]
    return out


def render_fr6_charts(
    *,
    fr4_campaign_metrics: pd.DataFrame,
    fr4_time_metrics: pd.DataFrame,
    fr4_repeat_metrics: pd.DataFrame,
    fr5_cohort_trend_metrics: Optional[pd.DataFrame] = None,
    fr5_cohort_comparison_metrics: Optional[pd.DataFrame] = None,
    out_dir: str,
) -> list[str]:
    """
    FR6: Generate static, dissertation-friendly charts from aggregate-only metric tables.

    Outputs:
      out/<run_id>/charts/*.png
      out/<run_id>/charts/manifest.csv

    Returns a list of generated file paths.
    """
    # Import matplotlib lazily so the rest of the pipeline can run even if charts are disabled.
    from matplotlib.ticker import PercentFormatter
    import numpy as np

    import matplotlib.dates as mdates

    import matplotlib as mpl

    _old_rcparams = mpl.rcParams.copy()
    mpl.rcParams.update(
        {
            # Transparent figure, dark axes so charts sit nicely on branded background
            "figure.facecolor": "none",
            "savefig.facecolor": "none",
            "savefig.edgecolor": "none",
            "savefig.transparent": True,
            "axes.facecolor": "#1b1337",
            "axes.edgecolor": "white",
            "axes.labelcolor": "white",
            "axes.titlecolor": "white",
            "xtick.color": "white",
            "ytick.color": "white",
            "text.color": "white",
            "grid.color": "white",
            "grid.alpha": 0.15,
            "legend.facecolor": "#1b1337",
            "legend.edgecolor": "white",
        }
    )

    def _tighten_date_xlim(ax, x_dt: pd.Series):
        if x_dt.empty:
            return
        xmin = x_dt.min()
        xmax = x_dt.max()
        pad = pd.Timedelta(days=20) if xmin == xmax else pd.Timedelta(days=10)
        ax.set_xlim(xmin - pad, xmax + pad)

    def _format_month_axis(ax, x_dt: pd.Series):
        """
        Use month ticks to avoid repeated labels like '2025-11' appearing multiple times
        when the range is only a few weeks.
        """
        if x_dt.empty:
            return
        span_days = (x_dt.max() - x_dt.min()).days if x_dt.notna().any() else 0

        # For up to ~2 years, show monthly ticks for clarity
        if span_days <= 730:
            ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        else:
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=12))

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.tick_params(axis="x", rotation=45)

    def _fmt_pct(v: float) -> str:
        return f"{v * 100:.1f}%"

    def _fmt_hours(v: float) -> str:
        return f"{v:.1f}h"

    def _annotate_points(
        ax,
        x_dt,
        y,
        *,
        fmt_fn,
        colour=None,
        dy_pts: int = 8,
        dx_pts: int = 0,
        every: int = 1,
        fontsize: int = 8,
    ):
        """
        Light-touch point labels. 'every' lets you label every Nth point.
        """
        x = pd.to_datetime(pd.Series(x_dt), errors="coerce")
        y_num = pd.to_numeric(pd.Series(y), errors="coerce")

        mask = x.notna() & y_num.notna()
        if mask.sum() == 0:
            return

        x = x[mask].tolist()
        y_num = y_num[mask].tolist()

        for i, (xi, yi) in enumerate(zip(x, y_num)):
            if every > 1 and (i % every) != 0:
                continue
            ax.annotate(
                fmt_fn(float(yi)),
                (xi, yi),
                textcoords="offset points",
                xytext=(dx_pts, dy_pts),
                ha="center",
                va="bottom" if dy_pts >= 0 else "top",
                fontsize=fontsize,
                color=colour,
            )

    def _annotate_trend_end(
        ax, x_end, y_end, *, fmt_fn, colour=None, fontsize: int = 8
    ):
        """
        Put a single value label at the end of the trendline/level line.
        """
        if x_end is None or y_end is None:
            return
        ax.annotate(
            fmt_fn(float(y_end)),
            (x_end, float(y_end)),
            textcoords="offset points",
            xytext=(8, 0),
            ha="left",
            va="center",
            fontsize=fontsize,
            color=colour,
        )

    def _add_linear_trendline(
        ax, x_dt, y, *, colour, label, allow_single_point: bool = True
    ):
        """
        Returns (x_end, y_end). If only one point exists and allow_single_point is True,
        draws a dotted level line rather than a misleading fitted trend.
        """
        x = pd.to_datetime(pd.Series(x_dt), errors="coerce")
        y_num = pd.to_numeric(pd.Series(y), errors="coerce")

        mask = x.notna() & y_num.notna()
        n = int(mask.sum())

        if n < 2:
            if n == 1 and allow_single_point:
                x0 = x[mask].iloc[0]
                y0 = float(y_num[mask].iloc[0])
                ax.plot(
                    [x0 - pd.Timedelta(days=10), x0 + pd.Timedelta(days=10)],
                    [y0, y0],
                    linestyle=":",
                    linewidth=2,
                    color=colour,
                    label=f"Level ({label})",
                )
                return x0, y0
            return None, None

        x_num = mdates.date2num(x[mask].tolist())
        y_fit_src = y_num[mask].to_numpy()

        m, c = np.polyfit(x_num, y_fit_src, 1)

        order = np.argsort(x_num)
        x_sorted_num = x_num[order]
        x_sorted_dt = pd.to_datetime([mdates.num2date(v) for v in x_sorted_num])
        y_sorted = (m * x_sorted_num + c).tolist()

        ax.plot(
            x_sorted_dt.tolist(),
            y_sorted,
            linestyle=":",
            linewidth=2,
            color=colour,
            label=f"Linear ({label})",
        )
        return x_sorted_dt.tolist()[-1], y_sorted[-1]

    charts_dir = Path(out_dir) / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    generated: list[str] = []
    manifest_rows: list[dict[str, str]] = []

    # -----------------------
    # 1) FR4 campaign trends
    # -----------------------
    cm = fr4_campaign_metrics.copy()

    if "campaign_year_month" not in cm.columns:
        cm["campaign_year_month"] = _derive_campaign_year_month_fr4(cm)

    cm["campaign_month_dt"] = pd.to_datetime(
        cm["campaign_year_month"].astype(str) + "-01", errors="coerce"
    )

    # One chart per technique to keep the story clear
    for technique, g in cm.groupby("technique", dropna=False):
        gg = g.sort_values(["campaign_month_dt", "campaign_id"], na_position="last")

        if gg.empty:
            continue

        gg = gg[gg["campaign_month_dt"].notna()].copy()
        x_dt = gg["campaign_month_dt"]

        fig, ax = plt.subplots(figsize=(10, 5))
        _tighten_date_xlim(ax, x_dt)

        series_added = 0

        for col, label in [
            ("read_rate", "Read rate"),
            ("interaction_rate", "Interaction rate"),
            ("report_rate", "Report rate"),
            ("compromise_rate", "Compromise rate"),
        ]:
            if col not in gg.columns:
                continue
            y = pd.to_numeric(gg[col], errors="coerce")
            if y.notna().any():
                (line,) = ax.plot(x_dt.tolist(), y.tolist(), marker="o", label=label)
                series_added += 1

                # Add point labels for key stakeholder metrics (keeps clutter down)
                if col in {"report_rate", "compromise_rate"}:
                    _annotate_points(
                        ax,
                        x_dt.tolist(),
                        y.tolist(),
                        fmt_fn=_fmt_pct,
                        colour=line.get_color(),
                        dy_pts=8,
                        fontsize=8,
                    )

                    x_end, y_end = _add_linear_trendline(
                        ax,
                        x_dt.tolist(),
                        y.tolist(),
                        colour=line.get_color(),
                        label=label,
                        allow_single_point=True,
                    )
                    _annotate_trend_end(
                        ax, x_end, y_end, fmt_fn=_fmt_pct, colour=line.get_color()
                    )

        if series_added == 0:
            plt.close(fig)
            continue

        ax.set_title(f"FR6: Behaviour rates over time ({technique})")
        ax.set_xlabel("Campaign")
        ax.set_ylabel("Rate (delivered denominator)")
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
        _format_month_axis(ax, x_dt)
        ax.grid(True, alpha=0.3)
        ax.legend()

        fig.tight_layout()
        fp = charts_dir / f"fr6_rates_trend_{technique}.png"
        fig.savefig(fp, dpi=200, transparent=True)
        plt.close(fig)

        generated.append(str(fp))
        manifest_rows.append(
            {
                "file": fp.name,
                "question": "How do read, interaction, report and compromise rates change over time?",
                "source": "fr4_campaign_metrics.csv",
            }
        )

    # -----------------------
    # 2) FR4 time-to-event trends
    # -----------------------
    tm = fr4_time_metrics.copy()
    if "campaign_year_month" not in tm.columns:
        tm["campaign_year_month"] = _derive_campaign_year_month_fr4(tm)

    tm["campaign_month_dt"] = pd.to_datetime(
        tm["campaign_year_month"].astype(str) + "-01", errors="coerce"
    )

    for metric_name in sorted(tm["metric"].dropna().unique()):
        gg = tm[tm["metric"] == metric_name].copy()
        if gg.empty:
            continue

        gg = gg.sort_values(["campaign_month_dt", "campaign_id"], na_position="last")

        gg = gg[gg["campaign_month_dt"].notna()].copy()
        x_dt = gg["campaign_month_dt"]

        median_hours = pd.to_numeric(gg["median_mins"], errors="coerce") / 60.0
        p90_hours = pd.to_numeric(gg["p90_mins"], errors="coerce") / 60.0

        if not (median_hours.notna().any() or p90_hours.notna().any()):
            continue

        fig, ax = plt.subplots(figsize=(10, 5))
        _tighten_date_xlim(ax, x_dt)

        (l1,) = ax.plot(
            x_dt.tolist(), median_hours.tolist(), marker="o", label="Median (hours)"
        )
        (l2,) = ax.plot(
            x_dt.tolist(), p90_hours.tolist(), marker="o", label="P90 (hours)"
        )

        # Point labels (median above, P90 below to reduce overlap)
        _annotate_points(
            ax,
            x_dt.tolist(),
            median_hours.tolist(),
            fmt_fn=_fmt_hours,
            colour=l1.get_color(),
            dy_pts=8,
        )
        _annotate_points(
            ax,
            x_dt.tolist(),
            p90_hours.tolist(),
            fmt_fn=_fmt_hours,
            colour=l2.get_color(),
            dy_pts=-10,
        )

        # Trend/level lines + end labels
        x_end, y_end = _add_linear_trendline(
            ax,
            x_dt.tolist(),
            median_hours.tolist(),
            colour=l1.get_color(),
            label="Median",
            allow_single_point=True,
        )
        _annotate_trend_end(ax, x_end, y_end, fmt_fn=_fmt_hours, colour=l1.get_color())

        x_end, y_end = _add_linear_trendline(
            ax,
            x_dt.tolist(),
            p90_hours.tolist(),
            colour=l2.get_color(),
            label="P90",
            allow_single_point=True,
        )
        _annotate_trend_end(ax, x_end, y_end, fmt_fn=_fmt_hours, colour=l2.get_color())

        ax.set_title(f"FR6: {metric_name} (median and P90)")
        ax.set_xlabel("Campaign")
        ax.set_ylabel("Hours from delivery")
        _format_month_axis(ax, x_dt)
        ax.grid(True, alpha=0.3)
        ax.legend()

        fig.tight_layout()
        fp = charts_dir / f"fr6_{metric_name}.png"
        fig.savefig(fp, dpi=200, transparent=True)
        plt.close(fig)

        generated.append(str(fp))
        manifest_rows.append(
            {
                "file": fp.name,
                "question": f"How quickly do users act? ({metric_name}, median vs tail behaviour)",
                "source": "fr4_time_metrics.csv",
            }
        )
    # -----------------------
    # 3) FR4 repeat behaviour distribution
    # -----------------------
    # Build a simple distribution: how many subjects interacted in 0, 1, 2, ... campaigns
    if fr4_repeat_metrics is not None and not fr4_repeat_metrics.empty:
        dist = (
            fr4_repeat_metrics.groupby("campaigns_interacted", as_index=False)[
                "n_subjects"
            ]
            .sum()
            .sort_values("campaigns_interacted")
        )

        total = int(dist["n_subjects"].sum())
        x_vals = dist["campaigns_interacted"].astype(int).tolist()
        y_vals = dist["n_subjects"].astype(int).tolist()

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(x_vals, y_vals)

        ax.set_title("FR6: Distribution of repeated interactions across campaigns")
        ax.set_xlabel("Number of campaigns with interaction")
        ax.set_ylabel("Number of subjects")

        # Make it understandable at a glance: show count and percentage on each bar
        y_max = max(y_vals) if y_vals else 0
        for x, y in zip(x_vals, y_vals):
            pct = (y / total * 100.0) if total else 0.0
            label = f"{y} ({pct:.1f}%)" if total else f"{y}"
            ax.text(
                x,
                y + (y_max * 0.015 if y_max else 0.5),
                label,
                ha="center",
                va="bottom",
                fontsize=9,
            )

        ax.set_xticks(x_vals)
        ax.grid(True, axis="y", alpha=0.25)

        fig.tight_layout()

        fp = charts_dir / "fr6_repeat_interactions_distribution.png"
        fig.savefig(fp, dpi=200, bbox_inches="tight", transparent=True)
        generated.append(str(fp))
        manifest_rows.append(
            {
                "file": fp.name,
                "question": "FR6: Distribution of repeated interactions across campaigns",
                "source": "fr4_repeat_metrics.csv",
            }
        )
        plt.close(fig)
    else:
        logger.info(
            "FR6 repeat-interactions chart skipped (no repeat metrics available)."
        )

    # -----------------------
    # 4) FR5 cohort trends (optional)
    # -----------------------
    if fr5_cohort_trend_metrics is not None and not fr5_cohort_trend_metrics.empty:
        ctm = fr5_cohort_trend_metrics.copy()
        ctm = ctm[ctm["campaign_year_month"].astype(str) != "ALL"].copy()
        ctm["campaign_month_dt"] = pd.to_datetime(
            ctm["campaign_year_month"].astype(str) + "-01", errors="coerce"
        )

        for technique, g in ctm.groupby("technique", dropna=False):
            gg = g.sort_values("campaign_month_dt", na_position="last")
            gg = gg[gg["campaign_month_dt"].notna()].copy()

            x_all = gg["campaign_month_dt"]
            fig, ax = plt.subplots(figsize=(10, 5))

            _tighten_date_xlim(ax, x_all)
            added = 0

            for cohort, h in gg.groupby("cohort", dropna=False):
                h = h.sort_values("campaign_month_dt", na_position="last")
                y = pd.to_numeric(h["report_rate"], errors="coerce")
                if y.notna().any():
                    (line,) = ax.plot(
                        h["campaign_month_dt"].tolist(),
                        y.tolist(),
                        marker="o",
                        label=f"Cohort {cohort}",
                    )

                    # Point labels
                    _annotate_points(
                        ax,
                        h["campaign_month_dt"].tolist(),
                        y.tolist(),
                        fmt_fn=_fmt_pct,
                        colour=line.get_color(),
                        dy_pts=8,
                        fontsize=8,
                    )

                    # Trend/level line + end label
                    x_end, y_end = _add_linear_trendline(
                        ax,
                        h["campaign_month_dt"].tolist(),
                        y.tolist(),
                        colour=line.get_color(),
                        label=f"Cohort {cohort}",
                        allow_single_point=True,
                    )
                    _annotate_trend_end(
                        ax, x_end, y_end, fmt_fn=_fmt_pct, colour=line.get_color()
                    )
                    added += 1

            if added == 0:
                plt.close(fig)
                continue

            ax.set_title(f"FR6: Report rate over time by cohort ({technique})")
            ax.set_xlabel("Campaign (YYYY-MM)")
            ax.set_ylabel("Report rate")
            ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
            _format_month_axis(ax, gg["campaign_month_dt"])
            ax.grid(True, alpha=0.3)
            ax.legend()

            fig.tight_layout()
            fp = charts_dir / f"fr6_cohort_report_rate_{technique}.png"
            fig.savefig(fp, dpi=200, transparent=True)
            plt.close(fig)

            generated.append(str(fp))
            manifest_rows.append(
                {
                    "file": fp.name,
                    "question": "Do cohorts A and B differ in reporting behaviour over time?",
                    "source": "fr5_cohort_trend_metrics.csv",
                }
            )

    # -----------------------
    # 5) FR5 cohort deltas (optional)
    # -----------------------
    if (
        fr5_cohort_comparison_metrics is not None
        and not fr5_cohort_comparison_metrics.empty
        and "report_rate_delta_A_minus_B" in fr5_cohort_comparison_metrics.columns
    ):
        cd = fr5_cohort_comparison_metrics.copy()
        cd = cd[cd["campaign_year_month"].astype(str) != "ALL"].copy()
        cd["campaign_month_dt"] = pd.to_datetime(
            cd["campaign_year_month"].astype(str) + "-01", errors="coerce"
        )

        for technique, g in cd.groupby("technique", dropna=False):
            gg = g.sort_values("campaign_month_dt", na_position="last")
            gg = gg[gg["campaign_month_dt"].notna()].copy()

            y = pd.to_numeric(gg["report_rate_delta_A_minus_B"], errors="coerce")
            if not y.notna().any():
                continue

            fig, ax = plt.subplots(figsize=(10, 4.5))
            ax.axhline(0, linewidth=1)
            ax.plot(gg["campaign_month_dt"].tolist(), y.tolist(), marker="o")

            ax.set_title(f"FR6: Report rate delta (A minus B) over time ({technique})")
            ax.set_xlabel("Campaign (YYYY-MM)")
            ax.set_ylabel("Delta (A minus B)")
            ax.yaxis.set_major_formatter(PercentFormatter(xmax=1.0))
            _format_month_axis(ax, gg["campaign_month_dt"])
            ax.grid(True, alpha=0.3)

            fig.tight_layout()
            fp = charts_dir / f"fr6_delta_report_rate_{technique}.png"
            fig.savefig(fp, dpi=200, transparent=True)
            plt.close(fig)

            generated.append(str(fp))
            manifest_rows.append(
                {
                    "file": fp.name,
                    "question": "How large is the reporting difference between cohorts (A minus B) over time?",
                    "source": "fr5_cohort_comparison_metrics.csv",
                }
            )

    # -----------------------
    # 6) Manifest
    # -----------------------
    if manifest_rows:
        manifest = pd.DataFrame(manifest_rows)
        manifest_path = charts_dir / "manifest.csv"
        manifest.to_csv(manifest_path, index=False)
        generated.append(str(manifest_path))

    logger.info("FR6 charts written to: %s", charts_dir)

    # Restore matplotlib defaults for callers outside FR6.
    try:
        import matplotlib as mpl  # type: ignore

        mpl.rcParams.update(_old_rcparams)
    except Exception:
        pass

    return generated
