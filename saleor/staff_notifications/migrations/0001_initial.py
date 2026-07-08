# Generated for ChapChap Tickets staff push notifications.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffDeviceToken",
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
                ("token", models.CharField(max_length=255, unique=True)),
                ("platform", models.CharField(blank=True, max_length=32)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="staff_device_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-last_seen_at",),
            },
        ),
        migrations.AddIndex(
            model_name="staffdevicetoken",
            index=models.Index(
                fields=["is_active", "last_seen_at"],
                name="staff_notif_is_acti_1f0f4b_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="staffdevicetoken",
            index=models.Index(
                fields=["user", "is_active"],
                name="staff_notif_user_id_4a3572_idx",
            ),
        ),
    ]
