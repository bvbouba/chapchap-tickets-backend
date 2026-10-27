import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="PrintJob",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("ses_message_id", models.CharField(max_length=255, unique=True)),
                ("sender", models.EmailField(max_length=254)),
                ("recipient", models.EmailField(max_length=254)),
                (
                    "bucket",
                    models.CharField(blank=True, default="", max_length=63),
                ),
                (
                    "object_key",
                    models.CharField(blank=True, default="", max_length=1024),
                ),
                (
                    "byte_size",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending_email", "Pending email"),
                            ("notified", "Notified"),
                            ("rejected", "Rejected"),
                            ("expired", "Expired"),
                        ],
                        default="pending_email",
                        max_length=32,
                    ),
                ),
                (
                    "failure_reason",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "code_hash",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("code_ciphertext", models.TextField(blank=True, default="")),
                ("failed_attempts", models.PositiveIntegerField(default=0)),
                ("locked_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(
                        fields=["status", "expires_at"],
                        name="printme_status_expires_idx",
                    )
                ],
            },
        ),
    ]
