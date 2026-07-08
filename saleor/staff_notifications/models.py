from django.conf import settings
from django.db import models
from django.utils import timezone


class StaffDeviceToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="staff_device_tokens",
        on_delete=models.CASCADE,
    )
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-last_seen_at",)
        indexes = [
            models.Index(fields=["is_active", "last_seen_at"]),
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.platform or 'unknown'}"
