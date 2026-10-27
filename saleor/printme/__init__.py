class PrintJobStatus:
    PENDING_EMAIL = "pending_email"
    NOTIFIED = "notified"
    REJECTED = "rejected"
    EXPIRED = "expired"

    CHOICES = [
        (PENDING_EMAIL, "Pending email"),
        (NOTIFIED, "Notified"),
        (REJECTED, "Rejected"),
        (EXPIRED, "Expired"),
    ]
