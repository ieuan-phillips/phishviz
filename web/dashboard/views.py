from pathlib import Path

from django.shortcuts import get_object_or_404, render

from runs.models import AnalysisRun, RunArtifact
from .services.metrics_reader import load_dashboard_data


def run_dashboard(request, run_id: str):
    run = get_object_or_404(AnalysisRun, run_id=run_id)

    selected_technique = request.GET.get("technique", "").strip()
    selected_period = request.GET.get("period", "").strip()

    dashboard_data = load_dashboard_data(
        Path(run.out_dir),
        selected_technique=selected_technique,
        selected_period=selected_period,
    )

    report_artifacts = run.artifacts.filter(
        artifact_type__in=[
            RunArtifact.ArtifactType.REPORT_PDF,
            RunArtifact.ArtifactType.REPORT_BUNDLE,
            RunArtifact.ArtifactType.RUN_CONFIG,
        ]
    )

    chart_artifacts = run.artifacts.filter(artifact_type=RunArtifact.ArtifactType.CHART)

    metric_artifacts = run.artifacts.filter(
        artifact_type=RunArtifact.ArtifactType.METRICS_CSV
    )

    return render(
        request,
        "dashboard/run_dashboard.html",
        {
            "run": run,
            "dashboard": dashboard_data,
            "report_artifacts": report_artifacts,
            "chart_artifacts": chart_artifacts,
            "metric_artifacts": metric_artifacts,
        },
    )
