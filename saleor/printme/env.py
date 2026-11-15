"""Parse PrintMe environment values without reading secrets back out."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def load_local_printme_env(path: Path | None = None) -> None:
    """Load gitignored ``printme.env`` without replacing existing variables.

    ``manage.py`` already loads ``.env``. Real PrintMe values stay in
    ``printme.env`` beside that file. A missing file changes nothing.
    Variables already set in the process or in ``.env`` win.
    """

    env_path = path
    if env_path is None:
        env_path = Path(__file__).resolve().parents[2] / "printme.env"
    if not env_path.is_file():
        return
    from dotenv import load_dotenv

    load_dotenv(env_path, override=False)


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
