"""Delete stored PDFs for print jobs whose access code has expired.

Run with ``python manage.py expire_print_jobs``. This command is not a
Celery task and is not added to ``CELERY_BEAT_SCHEDULE``.
"""

from django.core.management.base import BaseCommand

from ...services.cleanup import expire_due_jobs
from ...services.storage import S3ObjectStorage


class Command(BaseCommand):
    help = "Expire due PrintMe jobs and delete their stored documents."

    def handle(self, *args, **options):
        expired = expire_due_jobs(storage=S3ObjectStorage())
        self.stdout.write(f"expired {expired}")
