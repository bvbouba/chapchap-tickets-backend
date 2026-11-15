from django.urls import path

from saleor.printme.kiosk.views import (
    kiosk,
    kiosk_clear,
    kiosk_preview,
    kiosk_print,
    kiosk_print_status,
    kiosk_quote,
    kiosk_session,
)

urlpatterns = [
    path("printme/kiosk/", kiosk, name="printme-kiosk"),
    path("printme/kiosk/session/", kiosk_session, name="printme-kiosk-session"),
    path("printme/kiosk/session/clear/", kiosk_clear, name="printme-kiosk-clear"),
    path(
        "printme/kiosk/documents/<uuid:document_id>/preview/",
        kiosk_preview,
        name="printme-kiosk-preview",
    ),
    path("printme/kiosk/quote/", kiosk_quote, name="printme-kiosk-quote"),
    path("printme/kiosk/prints/", kiosk_print, name="printme-kiosk-print"),
    path(
        "printme/kiosk/prints/<uuid:submission_id>/",
        kiosk_print_status,
        name="printme-kiosk-print-status",
    ),
]
