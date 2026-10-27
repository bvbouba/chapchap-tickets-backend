"""Parse PrintMe environment values without reading secrets back out."""

import os

from django.core.exceptions import ImproperlyConfigured


def printme_int(name: str, default: int, *, minimum: int) -> int:
    """Return an integer setting. An empty variable uses ``default``.

    A non-integer, or a value below ``minimum``, stops Django startup. The
    invalid text is not included in the error.
    """

    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ImproperlyConfigured(f"{name} must be an integer") from None
    if value < minimum:
        raise ImproperlyConfigured(f"{name} must be {minimum} or greater") from None
    return value
