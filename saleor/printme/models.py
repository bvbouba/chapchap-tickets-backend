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
    document_name = models.CharField(max_length=1024, blank=True, default="")
    byte_size = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=PrintJobStatus.CHOICES,
        default=PrintJobStatus.PENDING_EMAIL,
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    rejected_files = models.TextField(blank=True, default="")
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


class PrintDocumentStatus:
    STORED = "stored"
    DELETED = "deleted"

    CHOICES = [
        (STORED, "Stored"),
        (DELETED, "Deleted"),
    ]


class PrintDocument(models.Model):
    """One printable file belonging to a print order.

    The original filename is display text only. The object key is a private
    identifier and is never taken from the sender's filename.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        PrintJob,
        related_name="documents",
        on_delete=models.CASCADE,
    )
    position = models.PositiveIntegerField()
    original_filename = models.CharField(max_length=255)
    source_format = models.CharField(max_length=8)
    printable_format = models.CharField(max_length=8, default="pdf")
    bucket = models.CharField(max_length=63, blank=True, default="")
    object_key = models.CharField(max_length=1024, blank=True, default="")
    byte_size = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=PrintDocumentStatus.CHOICES,
        default=PrintDocumentStatus.STORED,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "position"],
                name="printme_document_position_uniq",
            ),
            models.UniqueConstraint(
                fields=["bucket", "object_key"],
                condition=~models.Q(object_key=""),
                name="printme_document_object_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["job", "position"],
                name="printme_document_job_pos_idx",
            ),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.status})"


class PrintSubmissionStatus:
    """Lifecycle of one kiosk print attempt.

    ``accepted`` means this server accepted the request. ``submitted`` means
    PrintNode accepted it. Neither status means the printer finished the pages.
    """

    ACCEPTED = "accepted"
    SUBMITTED = "submitted"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"

    CHOICES = [
        (ACCEPTED, "Accepted"),
        (SUBMITTED, "Submitted"),
        (PRINTING, "Printing"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    ]
    ACTIVE = (ACCEPTED, SUBMITTED, PRINTING)


class PrintSubmission(models.Model):
    """One request to print a stored document on the configured printer.

    The row id is the idempotency key. PrintNode's job id and the printer id
    are recorded here and are not returned to the browser.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        PrintJob,
        related_name="submissions",
        on_delete=models.CASCADE,
    )
    document = models.ForeignKey(
        PrintDocument,
        related_name="submissions",
        on_delete=models.CASCADE,
    )
    printnode_job_id = models.PositiveBigIntegerField(null=True, blank=True)
    printer_id = models.PositiveBigIntegerField(null=True, blank=True)
    copies = models.PositiveSmallIntegerField()
    pages = models.CharField(max_length=128)
    color_mode = models.CharField(max_length=32)
    sides = models.CharField(max_length=32)
    currency = models.CharField(max_length=3, blank=True, default="")
    quoted_total = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    billable_quantity = models.PositiveIntegerField(null=True, blank=True)
    paper_size = models.CharField(max_length=16, blank=True, default="")
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    price_variant_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=PrintSubmissionStatus.CHOICES,
        default=PrintSubmissionStatus.ACCEPTED,
    )
    error_detail = models.CharField(max_length=64, blank=True, default="")
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["job", "document", "status"],
                name="printme_submit_status_idx",
            ),
        ]

    def __str__(self):
        return f"{self.id} ({self.status})"


class PrintPriceRate(models.Model):
    """One configured rate for a printed side or sheet.

    No active row means the kiosk says pricing is not yet available.
    ``amount`` is the price of one billable unit in ``currency``. ``unit``
    is ``side`` (one printed face) or ``sheet`` (one piece of paper).
    Two-sided sheet pricing uses one sheet for every two document pages.
    """

    paper_size = models.CharField(max_length=16, default="letter")
    color_mode = models.CharField(max_length=32)
    sides = models.CharField(max_length=32)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    unit = models.CharField(max_length=8, default="side")
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["paper_size", "color_mode", "sides"],
                condition=models.Q(active=True),
                name="printme_price_rate_active_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.paper_size} {self.color_mode} {self.sides}"
