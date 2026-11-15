from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("printme", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="printjob",
            name="document_name",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
    ]
