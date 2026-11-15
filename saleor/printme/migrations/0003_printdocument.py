import uuid

from django.db import migrations, models


def backfill_documents(apps, schema_editor):
    from saleor.printme.backfill import backfill_print_documents

    backfill_print_documents(
        apps.get_model("printme", "PrintJob"),
        apps.get_model("printme", "PrintDocument"),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("printme", "0002_printjob_document_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="printjob",
            name="rejected_files",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="PrintDocument",
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
                ("position", models.PositiveIntegerField()),
                ("original_filename", models.CharField(max_length=255)),
                ("source_format", models.CharField(max_length=8)),
                ("printable_format", models.CharField(default="pdf", max_length=8)),
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
                        choices=[("stored", "Stored"), ("deleted", "Deleted")],
                        default="stored",
                        max_length=16,
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="documents",
                        to="printme.printjob",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="printdocument",
            constraint=models.UniqueConstraint(
                fields=("job", "position"),
                name="printme_document_position_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="printdocument",
            constraint=models.UniqueConstraint(
                condition=~models.Q(object_key=""),
                fields=("bucket", "object_key"),
                name="printme_document_object_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="printdocument",
            index=models.Index(
                fields=["job", "position"],
                name="printme_document_job_pos_idx",
            ),
        ),
        migrations.RunPython(backfill_documents, migrations.RunPython.noop),
    ]
