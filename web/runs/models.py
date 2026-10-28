from django.db import models


class AnalysisRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        VALIDATED = "validated", "Validated"

    class SourceMode(models.TextChoices):
        UPLOAD = "upload", "Upload"
        REPRODUCE = "reproduce", "Reproduce"

    run_id = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    source_mode = models.CharField(
        max_length=16,
        choices=SourceMode.choices,
        default=SourceMode.UPLOAD,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    out_dir = models.CharField(max_length=500, blank=True)
    config_dir = models.CharField(max_length=500, blank=True)
    run_config_path = models.CharField(max_length=500, blank=True)

    accepted_files_count = models.PositiveIntegerField(default=0)
    rejected_files_count = models.PositiveIntegerField(default=0)
    warnings_count = models.PositiveIntegerField(default=0)

    normalised_rows = models.PositiveIntegerField(default=0)
    normalised_cols = models.PositiveIntegerField(default=0)

    cohort_enabled = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)

    stage_summary_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.run_id} ({self.status})"


class RunInput(models.Model):
    class Variant(models.TextChoices):
        CREDENTIAL_HARVEST = "credential_harvest", "Credential Harvest"
        MALWARE_ATTACHMENT = "malware_attachment", "Malware Attachment"
        UNKNOWN = "unknown", "Unknown"

    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.CASCADE,
        related_name="inputs",
    )

    original_name = models.CharField(max_length=255)
    stored_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.BigIntegerField(default=0)

    detected_variant = models.CharField(
        max_length=32,
        choices=Variant.choices,
        default=Variant.UNKNOWN,
    )
    accepted = models.BooleanField(default=False)
    message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["original_name"]

    def __str__(self) -> str:
        return f"{self.original_name} ({'accepted' if self.accepted else 'rejected'})"


class RunArtifact(models.Model):
    class ArtifactType(models.TextChoices):
        REPORT_PDF = "report_pdf", "Report PDF"
        REPORT_BUNDLE = "report_bundle", "Report Bundle"
        RUN_CONFIG = "run_config", "Run Config"
        CHART = "chart", "Chart"
        METRICS_CSV = "metrics_csv", "Metrics CSV"
        CONFIG_SNAPSHOT = "config_snapshot", "Config Snapshot"
        OTHER = "other", "Other"

    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    artifact_type = models.CharField(
        max_length=32,
        choices=ArtifactType.choices,
        default=ArtifactType.OTHER,
    )
    name = models.CharField(max_length=255)
    relative_path = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    is_downloadable = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["artifact_type", "name"]

    def __str__(self) -> str:
        return f"{self.run.run_id} :: {self.name}"
