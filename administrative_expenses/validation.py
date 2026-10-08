from io import BytesIO
from pathlib import PurePosixPath

import fitz
from PIL import Image, UnidentifiedImageError
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_BATCH_SIZE = 25 * 1024 * 1024
FORMATS = {".pdf": "PDF", ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}


def validate_upload(upload):
    name = str(upload.name)
    extension = PurePosixPath(name).suffix.lower()
    if (extension not in FORMATS or len(name) > 255 or "/" in name or "\\" in name
            or any(ord(char) < 32 for char in name) or name.startswith(".")):
        raise ValidationError(_("Only PDF, JPG, JPEG and PNG files with safe filenames are allowed."))
    if not upload.size or upload.size > MAX_FILE_SIZE:
        raise ValidationError(_("Each file must be non-empty and no larger than 10 MB."))
    position = upload.tell()
    try:
        upload.seek(0)
        data = upload.read(MAX_FILE_SIZE + 1)
        if len(data) != upload.size or len(data) > MAX_FILE_SIZE:
            raise ValueError
        if extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise ValueError
            with fitz.open(stream=data, filetype="pdf") as document:
                if not document.is_pdf or document.is_encrypted or document.page_count < 1:
                    raise ValueError
        else:
            with Image.open(BytesIO(data)) as image:
                if image.format != FORMATS[extension]:
                    raise ValueError
                image.verify()
    except (ValueError, RuntimeError, OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValidationError(_("The file content does not match a valid supported document format.")) from exc
    finally:
        upload.seek(position)
    return upload


def validate_uploads(uploads):
    if sum(file.size for file in uploads) > MAX_BATCH_SIZE:
        raise ValidationError(_("The total upload size cannot exceed 25 MB."))
    for upload in uploads:
        validate_upload(upload)
    return uploads
