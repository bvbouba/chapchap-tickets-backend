import subprocess
from pathlib import Path

import pytest

from saleor.printme.services import convert
from saleor.printme.services.convert import ConversionError, LibreOfficeConverter


def _track_directories(monkeypatch):
    created = []
    real_directory = convert.tempfile.TemporaryDirectory

    def tracking(*args, **kwargs):
        directory = real_directory(*args, **kwargs)
        created.append(directory.name)
        return directory

    monkeypatch.setattr(convert.tempfile, "TemporaryDirectory", tracking)
    return created


def test_pdf_is_returned_without_starting_libreoffice(monkeypatch):
    # given
    def fail(*args, **kwargs):
        raise AssertionError("soffice started")

    monkeypatch.setattr(subprocess, "run", fail)

    # when
    pdf = LibreOfficeConverter().to_pdf(b"%PDF-1.4\n", source_format="pdf")

    # then
    assert pdf == b"%PDF-1.4\n"


def test_docx_conversion_accepts_pdf_output_and_removes_the_temp_dir(monkeypatch):
    # given
    created = _track_directories(monkeypatch)

    def fake_run(argv, **kwargs):
        assert kwargs["timeout"] == 60
        assert "shell" not in kwargs
        assert argv[0] == "soffice"
        assert "--headless" in argv
        (Path(kwargs["cwd"]) / "input.pdf").write_bytes(b"%PDF-1.4\nconverted\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(convert.subprocess, "run", fake_run)

    # when
    pdf = LibreOfficeConverter().to_pdf(b"PK-docx", source_format="docx")

    # then
    assert pdf.startswith(b"%PDF-")
    assert created
    assert not Path(created[0]).exists()


def test_conversion_failure_removes_the_temp_dir(monkeypatch):
    # given
    created = _track_directories(monkeypatch)

    def time_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(convert.subprocess, "run", time_out)

    # when / then
    with pytest.raises(ConversionError):
        LibreOfficeConverter().to_pdf(b"PK-docx", source_format="docx")
    assert created
    assert not Path(created[0]).exists()


def test_output_that_is_not_a_pdf_is_rejected(monkeypatch):
    # given
    def fake_run(argv, **kwargs):
        (Path(kwargs["cwd"]) / "input.pdf").write_bytes(b"not a pdf")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(convert.subprocess, "run", fake_run)

    # when / then
    with pytest.raises(ConversionError):
        LibreOfficeConverter().to_pdf(b"PK-docx", source_format="docx")
