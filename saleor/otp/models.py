from django.db import models
from django.utils import timezone

class PasswordResetToken(models.Model):
    phone_number = models.CharField(max_length=20)
    token = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_valid(self):
        # Optional: tokens expire after 1 hour
        valid_duration = timezone.now() - timezone.timedelta(hours=1)
        return self.created_at >= valid_duration and not self.is_used