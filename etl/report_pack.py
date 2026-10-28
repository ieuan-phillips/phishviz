from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import pandas as pd
import numpy as np
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
    PageBreak,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


@dataclass
class ExecSummary:
    delivered: int
    read: int
    interacted: int
    reported: int
    compromised: int


def _infer_reporting_window(campaign_metrics: pd.DataFrame) -> str:
    if campaign_metrics is None or campaign_metrics.empty:
        return "Reporting window: latest processed run"

    candidates: List[str] = []

    if "campaign" in campaign_metrics.columns:
        candidates = [
            str(x) for x in campaign_metrics["campaign"].dropna().unique().tolist()
        ]
    elif (
        "campaign_year_code" in campaign_metrics.columns
        and "campaign_month" in campaign_metrics.columns
    ):
        # campaign_year_code is typically like 'P2025'
        years = (
            campaign_metrics["campaign_year_code"]
            .astype(str)
            .str.extract(r"(\d{4})")[0]
        )

        # campaign_month is typically a month name ('August'), but allow numeric months too
        months = campaign_metrics["campaign_month"].astype(str)
        month_map = {
            "jan": "01",
            "january": "01",
            "feb": "02",
            "february": "02",
            "mar": "03",
            "march": "03",
            "apr": "04",
            "april": "04",
            "may": "05",
            "jun": "06",
            "june": "06",
            "jul": "07",
            "july": "07",
            "aug": "08",
            "august": "08",
            "sep": "09",
            "sept": "09",
            "september": "09",
            "oct": "10",
            "october": "10",
            "nov": "11",
            "november": "11",
            "dec": "12",
            "december": "12",
        }

        def _to_mm(v: str) -> str | None:
            s = str(v).strip().lower()
            if s.isdigit():
                m = int(s)
                if 1 <= m <= 12:
                    return f"{m:02d}"
            return month_map.get(s) or month_map.get(s[:3])

        mm = months.map(_to_mm)
        if mm.notna().any():
            candidates = [
                f"{y}-{m}" for y, m in zip(years.fillna(""), mm.fillna("")) if y and m
            ]

    yms = []
    for c in candidates:
        m = re.match(r"^(\d{4})-(\d{2})$", str(c).strip())
        if m:
            yms.append(c)

    if not yms:
        return "Reporting window: latest processed run"

    yms = sorted(set(yms))
    if len(yms) == 1:
        return f"Reporting window: {yms[0]}"
    return f"Reporting window: {yms[0]} to {yms[-1]}"


def _safe_int(x) -> int:
    if x is None:
        return 0
    if isinstance(x, float) and np.isnan(x):
        return 0
    try:
        return int(round(float(x)))
    except Exception:
        return 0


def _build_exec_summary(campaign_metrics: pd.DataFrame) -> ExecSummary:
    """
    Prefer explicit count columns if present.
    Otherwise derive counts from rates using n_delivered as the denominator.
    """
    if campaign_metrics is None or campaign_metrics.empty:
        return ExecSummary(0, 0, 0, 0, 0)

    df = campaign_metrics.copy()
    delivered = _safe_int(df.get("n_delivered", pd.Series(dtype=float)).sum())

    explicit_map = {
        "read": ["n_read", "read"],
        "interacted": ["n_interacted", "interacted"],
        "reported": ["n_reported", "reported"],
        "compromised": ["n_compromised", "compromised"],
    }

    explicit = {}
    for k, cols in explicit_map.items():
        found = next((c for c in cols if c in df.columns), None)
        if found:
            explicit[k] = _safe_int(df[found].sum())

    if len(explicit) == 4:
        return ExecSummary(
            delivered=delivered,
            read=explicit["read"],
            interacted=explicit["interacted"],
            reported=explicit["reported"],
            compromised=explicit["compromised"],
        )

    n_del = df.get("n_delivered", pd.Series(dtype=float)).fillna(0).astype(float)

    def derived(rate_col: str) -> int:
        if rate_col not in df.columns:
            return 0
        r = df[rate_col].fillna(0).astype(float)
        return _safe_int((r * n_del).sum())

    read = explicit.get("read", derived("read_rate"))
    interacted = explicit.get("interacted", derived("interaction_rate"))
    reported = explicit.get("reported", derived("report_rate"))
    compromised = explicit.get("compromised", derived("compromise_rate"))

    return ExecSummary(delivered, read, interacted, reported, compromised)


def _rate(n: int, d: int) -> float:
    return (n / d) if d else 0.0


def _load_manifest_files(out_dir: Path) -> List[str]:
    candidates = [
        out_dir / "charts" / "manifest.csv",
        out_dir / "manifest.csv",
    ]
    manifest_path = next((p for p in candidates if p.exists()), None)
    if manifest_path is None:
        return []

    df = pd.read_csv(manifest_path)
    if "file" not in df.columns:
        return []

    return [str(x) for x in df["file"].dropna().tolist()]


def _draw_full_page_background(canvas, doc, bg_path: Path) -> None:
    if not bg_path.exists():
        return

    page_w, page_h = doc.pagesize
    try:
        img = ImageReader(str(bg_path))
        iw, ih = img.getSize()
        if iw <= 0 or ih <= 0:
            return

        # Cover: scale to fill page, then centre crop
        scale = max(page_w / iw, page_h / ih)
        dw, dh = iw * scale, ih * scale
        x = (page_w - dw) / 2.0
        y = (page_h - dh) / 2.0

        canvas.saveState()
        canvas.drawImage(img, x, y, width=dw, height=dh, mask="auto")
        canvas.restoreState()
    except Exception:
        return


def _draw_page_number(canvas, doc, colour=colors.white) -> None:
    page = canvas.getPageNumber()
    page_w, _ = doc.pagesize
    canvas.saveState()
    canvas.setFillColor(colour)
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(page_w - 2 * cm, 1.2 * cm, f"{page}")
    canvas.restoreState()


def _build_exec_summary_table(summary: ExecSummary) -> Table:
    data = [
        ["Metric", "Count", "Rate (delivered)"],
        ["Total delivered", f"{summary.delivered:,}", ""],
        [
            "Total read",
            f"{summary.read:,}",
            f"{_rate(summary.read, summary.delivered):.1%}",
        ],
        [
            "Total interacted",
            f"{summary.interacted:,}",
            f"{_rate(summary.interacted, summary.delivered):.1%}",
        ],
        [
            "Total reported",
            f"{summary.reported:,}",
            f"{_rate(summary.reported, summary.delivered):.1%}",
        ],
        [
            "Total compromised",
            f"{summary.compromised:,}",
            f"{_rate(summary.compromised, summary.delivered):.1%}",
        ],
    ]

    table = Table(data, colWidths=[7 * cm, 4 * cm, 4.5 * cm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.white),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.Color(1, 1, 1, alpha=0.6)),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.Color(1, 1, 1, alpha=0.25)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def render_fr7_report_pack(
    out_dir: Path,
    campaign_metrics: pd.DataFrame,
    *,
    title: str = "PhishViz: Phishing Simulation Report Pack",
    include_appendix: bool = True,
    build_zip_bundle: bool = True,
    background_image_path: Optional[Path] = None,
) -> Path:
    """
    FR7: EXPORTABLE REPORT PACK

    Builds a stakeholder-friendly PDF report pack from FR4/FR5 tables and FR6 charts.

    Outputs:
      - out_dir/report_pack.pdf
      - out_dir/report_pack_bundle.zip (optional)
    """
    out_dir = Path(out_dir)
    charts_dir = out_dir / "charts"
    pdf_path = out_dir / "report_pack.pdf"

    default_bg = (
        Path(__file__).resolve().parents[1] / "assets" / "report_background.png"
    )
    bg_path = Path(background_image_path) if background_image_path else default_bg

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="TitleLarge",
            parent=styles["Heading1"],
            fontSize=20,
            leading=24,
            spaceAfter=12,
            textColor=colors.white,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H2",
            parent=styles["Heading2"],
            fontSize=14,
            leading=18,
            spaceAfter=8,
            textColor=colors.white,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodyTextWhite",
            parent=styles["BodyText"],
            fontSize=11,
            leading=14,
            textColor=colors.white,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodySmall",
            parent=styles["BodyText"],
            fontSize=9,
            leading=11,
            textColor=colors.Color(1, 1, 1, alpha=0.85),
        )
    )

    reporting_window = _infer_reporting_window(campaign_metrics)
    summary = _build_exec_summary(campaign_metrics)

    manifest_files = _load_manifest_files(out_dir)
    if manifest_files:
        available = [charts_dir / f for f in manifest_files]
    else:
        available = sorted(charts_dir.glob("fr6_*.png"))
    file_map: Dict[str, Path] = {p.name: p for p in available if p.exists()}

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=3 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    def _on_page(canvas, doc):
        _draw_full_page_background(canvas, doc, bg_path)
        _draw_page_number(canvas, doc, colour=colors.Color(1, 1, 1, alpha=0.85))

    story: list = []

    story.append(Paragraph(title, styles["TitleLarge"]))
    story.append(Paragraph(reporting_window, styles["BodyTextWhite"]))
    story.append(
        Paragraph(
            f"Generated: {datetime.utcnow():%Y-%m-%d %H:%M} UTC", styles["BodySmall"]
        )
    )
    story.append(Spacer(1, 12))

    story.append(Paragraph("Executive summary (aggregate)", styles["H2"]))
    story.append(_build_exec_summary_table(summary))
    story.append(Spacer(1, 12))
    story.append(
        Paragraph(
            "Notes: Outputs are aggregate-only at campaign and cohort level. Rates use delivered as the denominator unless stated. "
            "Trend lines show a simple linear fit to support interpretation and do not imply causation.",
            styles["BodySmall"],
        )
    )
    story.append(PageBreak())

    def add_chart_section(section_title: str, filenames: List[str], description: str):
        story.append(Paragraph(section_title, styles["H2"]))
        story.append(Paragraph(description, styles["BodySmall"]))
        story.append(Spacer(1, 8))

        paths: List[Path] = []
        for fn in filenames:
            p = file_map.get(fn)
            if p:
                paths.append(p)

        if not paths:
            story.append(
                Paragraph("No charts available for this section.", styles["BodySmall"])
            )
            story.append(PageBreak())
            return

        for i, p in enumerate(paths):
            tmp_img = Image(str(p))
            ratio = (
                (tmp_img.imageHeight / tmp_img.imageWidth)
                if tmp_img.imageWidth
                else 0.5
            )
            target_w = min(doc.width, 16.0 * cm)
            img = Image(str(p), width=target_w, height=target_w * ratio)
            img.hAlign = "CENTER"
            story.append(img)
            story.append(Spacer(1, 10))

            # One chart per page for consistent layout.
            if i != len(paths) - 1:
                story.append(PageBreak())
                story.append(Paragraph(section_title, styles["H2"]))
                story.append(Paragraph(description, styles["BodySmall"]))
                story.append(Spacer(1, 8))

        story.append(PageBreak())

    add_chart_section(
        "Behaviour rates over time",
        [
            "fr6_rates_trend_credential_harvest.png",
            "fr6_rates_trend_malware_attachment.png",
        ],
        "How do read, interaction, report and compromise rates change over time (by technique)?",
    )

    add_chart_section(
        "Time-to-action (median and P90)",
        [
            "fr6_time_to_read_mins.png",
            "fr6_time_to_click_mins.png",
            "fr6_time_to_report_mins.png",
            "fr6_time_to_credential_submit_mins.png",
            "fr6_time_to_attachment_open_mins.png",
        ],
        "How quickly do users act? Median shows typical behaviour, while P90 reflects slower tail behaviour.",
    )

    add_chart_section(
        "Repeat interactions",
        ["fr6_repeat_interactions_distribution.png"],
        "How many subjects interacted across multiple campaigns?",
    )

    add_chart_section(
        "Cohort reporting over time",
        [
            "fr6_cohort_report_rate_credential_harvest.png",
            "fr6_cohort_report_rate_malware_attachment.png",
        ],
        "Do cohorts A and B differ in reporting behaviour over time?",
    )

    if include_appendix:
        used = set()
        for sec in story:
            if isinstance(sec, Image):
                used.add(Path(sec.filename).name)

        remaining = [
            p for p in available if p.name.startswith("fr6_") and p.name not in used
        ]
        if remaining:
            story.append(Paragraph("Appendix: Additional figures", styles["H2"]))
            story.append(
                Paragraph(
                    "Additional figures produced by FR6 that were not referenced in the main sections.",
                    styles["BodySmall"],
                )
            )
            story.append(Spacer(1, 10))

            for p in remaining:
                tmp_img = Image(str(p))
                ratio = (
                    (tmp_img.imageHeight / tmp_img.imageWidth)
                    if tmp_img.imageWidth
                    else 0.5
                )
                target_w = min(doc.width, 16.0 * cm)
                img = Image(str(p), width=target_w, height=target_w * ratio)
                img.hAlign = "CENTER"
                story.append(img)
                story.append(Spacer(1, 10))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    logger.info("FR7 report pack written to: %s", pdf_path)

    if build_zip_bundle:
        zip_path = build_fr7_bundle(out_dir=out_dir, background_image_path=bg_path)
        logger.info("FR7 bundle written to: %s", zip_path)

    return pdf_path


def build_fr7_bundle(
    *, out_dir: Path, background_image_path: Optional[Path] = None
) -> Path:
    """
    Builds (or rebuilds) the FR7 zip bundle.

    This is useful for FR8, where run_config.json is finalised after the PDF is generated,
    and should be included in the bundle.

    Contents (when present):
      - report_pack.pdf
      - charts/*.png
      - charts/manifest.csv
      - run_config.json
      - config_snapshot/** (copied from config/)
      - assets/report background image (if available)
    """
    out_dir = Path(out_dir)
    charts_dir = out_dir / "charts"
    pdf_path = out_dir / "report_pack.pdf"
    zip_path = out_dir / "report_pack_bundle.zip"

    bg_path = Path(background_image_path) if background_image_path else None

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if pdf_path.exists():
            z.write(pdf_path, arcname=pdf_path.name)

        # Charts + manifest
        if charts_dir.exists():
            for p in sorted(charts_dir.glob("*.png")):
                z.write(p, arcname=f"charts/{p.name}")

            manifest_path = charts_dir / "manifest.csv"
            if manifest_path.exists():
                z.write(manifest_path, arcname="charts/manifest.csv")

        # FR8: run configuration
        run_cfg = out_dir / "run_config.json"
        if run_cfg.exists():
            z.write(run_cfg, arcname="run_config.json")

        # FR8: config snapshot
        snap = out_dir / "config_snapshot"
        if snap.exists():
            for p in sorted(snap.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(snap).as_posix()
                    z.write(p, arcname=f"config_snapshot/{rel}")

        # Background asset (optional)
        if bg_path and bg_path.exists():
            z.write(bg_path, arcname=f"assets/{bg_path.name}")

    return zip_path
