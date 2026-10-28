from django.contrib import admin
from .models import AnalysisRun, RunInput, RunArtifact


@admin.register(AnalysisRun)
class AnalysisRunAdmin(admin.ModelAdmin):
    list_display = (
        "run_id",
        "status",
        "source_mode",
        "accepted_files_count",
        "rejected_files_count",
        "warnings_count",
        "cohort_enabled",
        "created_at",
        "finished_at",
    )
    search_fields = ("run_id", "label")
    list_filter = ("status", "source_mode", "cohort_enabled")


@admin.register(RunInput)
class RunInputAdmin(admin.ModelAdmin):
    list_display = (
        "original_name",
        "run",
        "detected_variant",
        "accepted",
        "size_bytes",
        "created_at",
    )
    search_fields = ("original_name", "run__run_id")
    list_filter = ("accepted", "detected_variant")


@admin.register(RunArtifact)
class RunArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "run",
        "artifact_type",
        "is_downloadable",
        "size_bytes",
        "created_at",
    )
    search_fields = ("name", "run__run_id")
    list_filter = ("artifact_type", "is_downloadable")
