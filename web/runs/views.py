from collections import OrderedDict
import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import NewRunForm
from .models import AnalysisRun, RunArtifact
from .services.pipeline_service import create_analysis_run, execute_analysis_run

from .services.reproduce_service import (
    create_reproduce_run,
    execute_reproduce_run,
    verify_run_against_config,
)


def _build_artifact_sections(run: AnalysisRun):
    sections = OrderedDict(
        [
            ("Reports", []),
            ("Provenance", []),
            ("Metric tables", []),
            ("Charts", []),
            ("Other", []),
        ]
    )

    for artifact in run.artifacts.all():
        if artifact.artifact_type in {
            RunArtifact.ArtifactType.REPORT_PDF,
            RunArtifact.ArtifactType.REPORT_BUNDLE,
        }:
            sections["Reports"].append(artifact)
        elif artifact.artifact_type in {
            RunArtifact.ArtifactType.RUN_CONFIG,
            RunArtifact.ArtifactType.CONFIG_SNAPSHOT,
        }:
            sections["Provenance"].append(artifact)
        elif artifact.artifact_type == RunArtifact.ArtifactType.METRICS_CSV:
            sections["Metric tables"].append(artifact)
        elif artifact.artifact_type == RunArtifact.ArtifactType.CHART:
            sections["Charts"].append(artifact)
        else:
            sections["Other"].append(artifact)

    return [(title, items) for title, items in sections.items() if items]


def run_list(request):
    runs = AnalysisRun.objects.all()
    return render(request, "runs/run_list.html", {"runs": runs})


def run_create(request):
    if request.method == "POST":
        form = NewRunForm(request.POST, request.FILES)
        files = request.FILES.getlist("csv_files")

        if form.is_valid() and files:
            project_root = Path(settings.BASE_DIR).parent

            run = create_analysis_run(
                run_label=form.cleaned_data["run_label"],
                uploaded_files=files,
                enable_cohort_mode=form.cleaned_data["enable_cohort_mode"],
                project_root=project_root,
            )

            run = execute_analysis_run(
                run=run,
                build_bundle=form.cleaned_data["build_bundle"],
                project_root=project_root,
            )

            return redirect("runs:detail", run_id=run.run_id)
    else:
        form = NewRunForm()

    return render(request, "runs/run_create.html", {"form": form})


def run_detail(request, run_id: str):
    run = get_object_or_404(AnalysisRun, run_id=run_id)
    artifact_sections = _build_artifact_sections(run)

    return render(
        request,
        "runs/run_detail.html",
        {
            "run": run,
            "artifact_sections": artifact_sections,
        },
    )


def artifact_download(request, run_id: str, artifact_id: int):
    run = get_object_or_404(AnalysisRun, run_id=run_id)
    artifact = get_object_or_404(
        RunArtifact,
        id=artifact_id,
        run=run,
        is_downloadable=True,
    )

    file_path = Path(artifact.relative_path)

    if not file_path.exists() or not file_path.is_file():
        raise Http404("Artifact file not found.")

    return FileResponse(
        file_path.open("rb"),
        as_attachment=True,
        filename=artifact.name,
    )


def artifact_view(request, run_id: str, artifact_id: int):
    run = get_object_or_404(AnalysisRun, run_id=run_id)
    artifact = get_object_or_404(
        RunArtifact,
        id=artifact_id,
        run=run,
        is_downloadable=True,
    )

    file_path = Path(artifact.relative_path)

    if not file_path.exists() or not file_path.is_file():
        raise Http404("Artifact file not found.")

    content_type, _ = mimetypes.guess_type(str(file_path))

    return FileResponse(
        file_path.open("rb"),
        as_attachment=False,
        filename=artifact.name,
        content_type=content_type or "application/octet-stream",
    )


def run_verify(request, run_id: str):
    run = get_object_or_404(AnalysisRun, run_id=run_id)
    project_root = Path(settings.BASE_DIR).parent

    verification = verify_run_against_config(
        run_config_path=Path(run.run_config_path),
        project_root=project_root,
    )

    return render(
        request,
        "runs/run_verify.html",
        {
            "run": run,
            "verification": verification,
        },
    )


def run_reproduce(request, run_id: str):
    source_run = get_object_or_404(AnalysisRun, run_id=run_id)

    if request.method != "POST":
        return redirect("runs:verify", run_id=source_run.run_id)

    project_root = Path(settings.BASE_DIR).parent

    verification = verify_run_against_config(
        run_config_path=Path(source_run.run_config_path),
        project_root=project_root,
    )

    if not verification.run_config_exists or not verification.is_valid:
        return render(
            request,
            "runs/run_verify.html",
            {
                "run": source_run,
                "verification": verification,
                "reproduce_error": "Reproduction is blocked until verification passes successfully.",
            },
        )

    reproduced_run = create_reproduce_run(
        source_run=source_run,
        project_root=project_root,
    )

    reproduced_run = execute_reproduce_run(
        run=reproduced_run,
        source_run=source_run,
        project_root=project_root,
    )

    return redirect("runs:detail", run_id=reproduced_run.run_id)
