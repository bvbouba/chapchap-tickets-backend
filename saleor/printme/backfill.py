"""Copy existing single-PDF jobs into PrintDocument rows.

Historical jobs keep their original columns. This function adds a child row
and does not change the job's code, status, or storage key.
"""

DOCUMENT_PREFIX = "raw/documents/"


def backfill_print_documents(print_job_model, print_document_model) -> int:
    """Create one stored PDF document for each legacy job that has a file."""

    created = 0
    jobs = print_job_model.objects.exclude(object_key="").iterator()
    for job in jobs:
        key = str(job.object_key or "")
        if not key.startswith(DOCUMENT_PREFIX):
            continue
        if print_document_model.objects.filter(job_id=job.id).exists():
            continue
        name = str(job.document_name or "").splitlines()
        filename = name[0].strip() if name and name[0].strip() else "document.pdf"
        print_document_model.objects.create(
            job_id=job.id,
            position=1,
            original_filename=filename[:255],
            source_format="pdf",
            printable_format="pdf",
            bucket=job.bucket or "",
            object_key=key,
            byte_size=job.byte_size,
            status="stored",
        )
        created += 1
    return created
