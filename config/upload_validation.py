from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError


PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
}


def validate_pdf_upload(uploaded_file):
    """Validate a newly uploaded PDF without retaining or rewriting its contents."""
    if uploaded_file is None:
        return

    name = str(getattr(uploaded_file, "name", ""))
    if Path(name).suffix.lower() != ".pdf":
        raise ValidationError("Only PDF files are accepted.")

    size = getattr(uploaded_file, "size", None)
    if size is not None:
        if size <= 0:
            raise ValidationError("The uploaded PDF is empty.")
        if size > settings.MAX_PDF_UPLOAD_SIZE:
            limit_mb = settings.MAX_PDF_UPLOAD_SIZE // (1024 * 1024)
            raise ValidationError(f"The PDF must not exceed {limit_mb} MB.")

    content_type = (
        str(getattr(uploaded_file, "content_type", "") or "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if content_type and content_type not in PDF_CONTENT_TYPES:
        raise ValidationError("The uploaded file does not have a PDF content type.")

    original_position = uploaded_file.tell() if hasattr(uploaded_file, "tell") else None
    try:
        uploaded_file.seek(0)
        signature = uploaded_file.read(5)
    finally:
        if original_position is not None:
            uploaded_file.seek(original_position)

    if signature != b"%PDF-":
        raise ValidationError("The uploaded file does not contain a valid PDF header.")
