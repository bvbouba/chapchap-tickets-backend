import uuid

from django.db import models

from . import PrintJobStatus


class PrintJob(models.Model):
    """One inbound email accepted or rejected for printing.

    Document bytes stay in private object storage. This row keeps the
    location, processing status, and access-code metadata.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ses_message_id = models.CharField(max_length=255, unique=True)
    sender = models.EmailField()
    recipient = models.EmailField()
    bucket = models.CharField(max_length=63, blank=True, default="")
    object_key = models.CharField(max_length=1024, blank=True, default="")
    byte_size = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=PrintJobStatus.CHOICES,
        default=PrintJobStatus.PENDING_EMAIL,
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    code_hash = models.CharField(max_length=255, blank=True, default="")
    code_ciphertext = models.TextField(blank=True, default="")
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=["status", "expires_at"],
                name="printme_status_expires_idx",
            ),
        ]

    def __str__(self):
        return f"{self.ses_message_id} ({self.status})"
