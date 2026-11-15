import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("printme", "0003_printdocument"),
    ]

    operations = [
        migrations.CreateModel(
            name="PrintSubmission",
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
                (
                    "printnode_job_id",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                (
                    "printer_id",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                ("copies", models.PositiveSmallIntegerField()),
                ("pages", models.CharField(max_length=32)),
                ("color_mode", models.CharField(max_length=32)),
                ("sides", models.CharField(max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("accepted", "Accepted"),
                            ("submitted", "Submitted"),
                            ("printing", "Printing"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        default="accepted",
                        max_length=16,
                    ),
                ),
                (
                    "error_detail",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="submissions",
                        to="printme.printdocument",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="submissions",
                        to="printme.printjob",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="printsubmission",
            index=models.Index(
                fields=["job", "document", "status"],
                name="printme_submit_status_idx",
            ),
        ),
    ]
