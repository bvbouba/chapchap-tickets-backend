from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("printme", "0004_printsubmission"),
    ]

    operations = [
        migrations.AlterField(
            model_name="printsubmission",
            name="pages",
            field=models.CharField(max_length=128),
        ),
        migrations.AddField(
            model_name="printsubmission",
            name="currency",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
        migrations.AddField(
            model_name="printsubmission",
            name="quoted_total",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True
            ),
        ),
        migrations.AddField(
            model_name="printsubmission",
            name="billable_quantity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="PrintPriceRate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("paper_size", models.CharField(default="letter", max_length=16)),
                ("color_mode", models.CharField(max_length=32)),
                ("sides", models.CharField(max_length=32)),
                ("currency", models.CharField(max_length=3)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("unit", models.CharField(default="side", max_length=8)),
                ("active", models.BooleanField(default=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="printpricerate",
            constraint=models.UniqueConstraint(
                condition=models.Q(active=True),
                fields=("paper_size", "color_mode", "sides"),
                name="printme_price_rate_active_uniq",
            ),
        ),
    ]
