import io
from email.message import EmailMessage

from saleor.printme.services.parse_email import (
    EmailRejection,
    parse_email,
)

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SENDER = "sender@example.com"
RECIPIENT = "print@in.bavasim.com"


def _message(parts, *, body="Please print the attached file."):
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = "print@in.bavasim.com"
    message["Subject"] = "Print job"
    message.set_content(body)
    for filename, content, maintype, subtype, cte in parts:
        message.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            cte=cte,
            filename=filename,
        )
    return message.as_bytes()


def _pdf_message(*, filename="notes.txt", cte="base64"):
    return _message(
        [(filename, PDF, "application", "octet-stream", cte)],
    )


def test_valid_pdf_attachment_ignores_filename_and_mime_type(caplog):
    # given
    base64_message = _pdf_message(cte="base64")
    quoted_message = _pdf_message(cte="quoted-printable")

    # when
    base64_result = parse_email(base64_message)
    quoted_result = parse_email(quoted_message)

    # then
    for result in (base64_result, quoted_result):
        assert result.ok is True
        assert result.failure_reason == ""
        assert result.sender == SENDER
        assert len(result.attachments) == 1
        assert result.attachments[0].filename == "notes.txt"
        assert result.attachments[0].content == PDF
    assert PDF not in caplog.text.encode()
    assert SENDER not in caplog.text


def test_missing_attachment_is_rejected():
    # given
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = "print@in.bavasim.com"
    message.set_content("The body mentions %PDF-1.4 but has no file.")

    # when
    result = parse_email(message.as_bytes())

    # then
    assert result.ok is False
    assert result.failure_reason == EmailRejection.MISSING_ATTACHMENT
    assert result.sender == SENDER
    assert result.attachments == ()


def test_non_pdf_attachment_is_rejected_even_when_labeled_as_pdf():
    # given
    raw = _message(
        [("report.pdf", b"This is not a PDF.", "application", "pdf", "base64")],
    )

    # when
    result = parse_email(raw)

    # then
    assert result.ok is True
    assert result.attachments == ()
    assert result.rejections[0].filename == "report.pdf"
    assert result.rejections[0].reason == "unsupported"
    assert b"This is not a PDF." not in result.failure_reason.encode()


def test_mixed_pdf_and_non_pdf_attachments_reject_the_whole_email():
    # given
    raw = _message(
        [
            ("job.pdf", PDF, "application", "pdf", "base64"),
            ("photo.png", b"\x89PNG\r\n\x1a\nnot-a-pdf", "image", "png", "base64"),
        ],
    )

    # when
    result = parse_email(raw)

    # then
    assert result.ok is True
    assert [item.filename for item in result.attachments] == ["job.pdf"]
    assert result.rejections[0].filename == "photo.png"
    assert result.rejections[0].reason == "unsupported"
    assert PDF not in result.failure_reason.encode()


def test_malformed_mime_message_is_rejected_safely():
    # given
    raw = (
        b"From: sender@example.com\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUND\r\n"
        b"\r\n"
        b"--NOT-THE-BOUNDARY\r\n"
    )

    # when
    result = parse_email(raw)

    # then
    assert result.ok is False
    assert result.failure_reason == EmailRejection.MALFORMED
    assert result.attachments == ()


def test_oversized_message_or_attachment_is_rejected_before_use():
    # given
    stream = io.BytesIO(b"y" * 80)
    small_pdf = _pdf_message()
    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = "print@in.bavasim.com"
    message.set_content("Print this")
    message.add_attachment(
        PDF, maintype="application", subtype="pdf", filename="job.pdf"
    )
    for part in message.walk():
        if part.get_filename() == "job.pdf":
            part["Content-Length"] = "5000000"
    declared_oversize = message.as_bytes()

    # when
    raw_result = parse_email(b"x" * 50, max_bytes=49)
    stream_result = parse_email(stream, max_bytes=20)
    message_result = parse_email(small_pdf, max_bytes=len(small_pdf) - 1)
    declared_result = parse_email(declared_oversize, max_bytes=len(declared_oversize))

    # then
    assert raw_result.failure_reason == EmailRejection.OVERSIZED
    assert stream_result.failure_reason == EmailRejection.OVERSIZED
    assert stream.tell() == 21
    assert message_result.failure_reason == EmailRejection.OVERSIZED
    assert declared_result.ok is True
    assert declared_result.attachments == ()
    assert declared_result.rejections[0].reason == "oversized"
    for result in (raw_result, stream_result, message_result):
        assert result.ok is False
        assert result.attachments == ()


def test_pdf_magic_must_fall_inside_the_first_window():
    # given
    late = b"x" * 1024 + b"%PDF-1.4\ntrailer<<>>\n%%EOF\n"
    early = b"x" * 1019 + b"%PDF-1.4\ntrailer<<>>\n%%EOF\n"
    late_message = _message(
        [("late.pdf", late, "application", "pdf", "base64")],
    )
    early_message = _message(
        [("early.pdf", early, "application", "octet-stream", "base64")],
    )

    # when
    late_result = parse_email(late_message)
    early_result = parse_email(early_message)

    # then
    assert late_result.ok is True
    assert late_result.attachments == ()
    assert late_result.rejections[0].reason == "unsupported"
    assert early_result.ok is True
    assert early_result.attachments[0].content == early


def test_attachment_filename_cannot_escape_as_a_path():
    # given
    raw = _message(
        [
            (
                "../../secret\x00.pdf",
                PDF,
                "application",
                "pdf",
                "base64",
            )
        ],
    )

    # when
    result = parse_email(raw)

    # then
    assert result.ok is True
    assert result.attachments[0].filename == "secret.pdf"
    assert "/" not in result.attachments[0].filename
    assert ".." not in result.attachments[0].filename


def test_sender_header_injection_is_not_accepted():
    # given
    raw = (
        b"From: sender@example.com\r\nBcc: evil@example.com\r\n"
        b"To: print@in.bavasim.com\r\n"
        b"Subject: Print job\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Please print the attached file.\r\n"
    )
    smuggled = (
        b"From: sender@example.com\nBcc: evil@example.com\r\n"
        b"To: print@in.bavasim.com\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Please print the attached file.\r\n"
    )

    # when
    extra_header = parse_email(raw)
    newline_sender = parse_email(smuggled)

    # then
    assert extra_header.failure_reason == EmailRejection.MISSING_ATTACHMENT
    assert extra_header.sender == SENDER
    assert "evil@example.com" not in extra_header.sender
    assert newline_sender.sender != "evil@example.com"
    assert "\n" not in newline_sender.sender
    assert "\r" not in newline_sender.sender
    assert newline_sender.attachments == ()


def test_docx_package_is_accepted_and_a_plain_zip_is_not():
    # given
    import io
    import zipfile

    word = io.BytesIO()
    with zipfile.ZipFile(word, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("readme.txt", "hello")
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message.set_content("Word and zip.")
    message.add_attachment(
        word.getvalue(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="letter.docx",
    )
    message.add_attachment(
        plain.getvalue(), maintype="application", subtype="zip", filename="notes.zip"
    )
    message.add_attachment(
        b"PK\x03\x04not-a-zip",
        maintype="application",
        subtype="octet-stream",
        filename="broken.docx",
    )

    # when
    result = parse_email(message.as_bytes())

    # then
    assert result.ok is True
    assert result.attachments[0].filename == "letter.docx"
    assert result.attachments[0].source_format == "docx"
    reasons = {item.filename: item.reason for item in result.rejections}
    assert reasons["notes.zip"] == "unsupported"
    assert reasons["broken.docx"] == "corrupt"


def test_eleventh_pdf_is_rejected_as_excess():
    # given
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message.set_content("Too many files.")
    for index in range(11):
        message.add_attachment(
            PDF,
            maintype="application",
            subtype="pdf",
            filename=f"file-{index}.pdf",
        )

    # when
    result = parse_email(message.as_bytes())

    # then
    assert result.ok is True
    assert len(result.attachments) == 10
    assert result.rejections[0].filename == "file-10.pdf"
    assert result.rejections[0].reason == "excess"


def test_attachment_over_the_per_file_limit_is_rejected_alone(monkeypatch):
    # given
    monkeypatch.setattr(
        "saleor.printme.services.parse_email._PER_FILE_BYTES",
        100,
    )
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message.set_content("One file is too large.")
    message.add_attachment(
        b"%PDF-ok", maintype="application", subtype="pdf", filename="small.pdf"
    )
    message.add_attachment(
        b"%PDF-" + (b"x" * 200),
        maintype="application",
        subtype="pdf",
        filename="large.pdf",
    )

    # when
    result = parse_email(message.as_bytes(), max_bytes=1024)

    # then
    assert result.ok is True
    assert [item.filename for item in result.attachments] == ["small.pdf"]
    assert result.rejections[0].filename == "large.pdf"
    assert result.rejections[0].reason == "oversized"


def test_string_input_is_rejected_as_malformed():
    # when
    result = parse_email("From: sender@example.com\r\n\r\n")  # type: ignore[arg-type]

    # then
    assert result.failure_reason == EmailRejection.MALFORMED
