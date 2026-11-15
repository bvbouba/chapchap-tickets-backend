"""Convert a Word document to PDF with LibreOffice.

The converter runs one document at a time. It does not use a shell, and it
does not log the document or the process output.
"""

import os
import subprocess
import tempfile
import threading
from pathlib import Path

_CONVERT_LOCK = threading.Lock()
_TIMEOUT_SECONDS = 60
_PDF_MAGIC = b"%PDF-"


class ConversionError(Exception):
    """The document could not be turned into a PDF."""


class LibreOfficeConverter:
    """Headless LibreOffice conversion."""

    def to_pdf(self, content: bytes, *, source_format: str) -> bytes:
        if source_format == "pdf":
            if not isinstance(content, (bytes, bytearray)) or not content.startswith(
                _PDF_MAGIC
            ):
                raise ConversionError("Document could not be converted")
            return bytes(content)
        if source_format != "docx":
            raise ConversionError("Document could not be converted")
        with _CONVERT_LOCK:
            return _convert_docx(bytes(content))


def _convert_docx(content: bytes) -> bytes:
    try:
        with tempfile.TemporaryDirectory(prefix="printme-") as directory:
            source = Path(directory) / "input.docx"
            profile = Path(directory) / "profile"
            source.write_bytes(content)
            profile.mkdir()
            completed = subprocess.run(
                [
                    "soffice",
                    "--headless",
                    "--norestore",
                    "--nologo",
                    "--nolockcheck",
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    directory,
                    str(source),
                ],
                cwd=directory,
                timeout=_TIMEOUT_SECONDS,
                capture_output=True,
                check=False,
                env={**os.environ, "HOME": directory},
            )
            output = Path(directory) / "input.pdf"
            if completed.returncode != 0 or not output.is_file():
                raise ConversionError("Document could not be converted")
            pdf = output.read_bytes()
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("Document could not be converted") from exc
    except OSError as exc:
        raise ConversionError("Document could not be converted") from exc
    if not pdf.startswith(_PDF_MAGIC):
        raise ConversionError("Document could not be converted")
    return pdf
