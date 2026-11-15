from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.views import serve
from django.urls import path, re_path
from django.views.decorators.csrf import csrf_exempt

from .core.views import jwks
from .graphql.api import backend, schema
from .graphql.views import GraphQLView
from .plugins.views import (
    handle_global_plugin_webhook,
    handle_plugin_per_channel_webhook,
    handle_plugin_webhook,
)
from .printme.kiosk.views import (
    kiosk,
    kiosk_clear,
    kiosk_preview,
    kiosk_print,
    kiosk_print_status,
    kiosk_quote,
    kiosk_session,
)
from .product.views import digital_product
from .thumbnail.views import handle_thumbnail

urlpatterns = [
    path('admin', admin.site.urls),
    re_path(
        r"^graphql/$",
        csrf_exempt(GraphQLView.as_view(backend=backend, schema=schema)),
        name="api",
    ),
    re_path(
        r"^digital-download/(?P<token>[0-9A-Za-z_\-]+)/$",
        digital_product,
        name="digital-product",
    ),
    re_path(
        r"^plugins/channel/(?P<channel_slug>[.0-9A-Za-z_\-]+)/"
        r"(?P<plugin_id>[.0-9A-Za-z_\-]+)/",
        handle_plugin_per_channel_webhook,
        name="plugins-per-channel",
    ),
    re_path(
        r"^plugins/global/(?P<plugin_id>[.0-9A-Za-z_\-]+)/",
        handle_global_plugin_webhook,
        name="plugins-global",
    ),
    re_path(
        r"^plugins/(?P<plugin_id>[.0-9A-Za-z_\-]+)/",
        handle_plugin_webhook,
        name="plugins",
    ),
    re_path(
        (
            r"^thumbnail/(?P<instance_id>[.0-9A-Za-z_=\-]+)/(?P<size>\d+)/"
            r"(?:(?P<format>[a-zA-Z]+)/)?"
        ),
        handle_thumbnail,
        name="thumbnail",
    ),
    re_path(r"^\.well-known/jwks.json$", jwks, name="jwks"),
    path("printme/kiosk/", kiosk, name="printme-kiosk"),
    path("printme/kiosk/session/", kiosk_session, name="printme-kiosk-session"),
    path(
        "printme/kiosk/session/clear/",
        kiosk_clear,
        name="printme-kiosk-clear",
    ),
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

if settings.DEBUG:
    from .core import views

    urlpatterns += static("/media/", document_root=settings.MEDIA_ROOT) + [
        re_path(r"^static/(?P<path>.*)$", serve),
        re_path(r"^$", views.home, name="home"),
    ]
