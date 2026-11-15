from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("printme", "0005_print_pricing"),
    ]

    operations = [
        migrations.AddField(
            model_name="printsubmission",
            name="paper_size",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="printsubmission",
            name="unit_price",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True
            ),
        ),
        migrations.AddField(
            model_name="printsubmission",
            name="price_variant_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
