import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.utils._os import safe_join


INLINE_CONTENT_TYPES = {
    "application/pdf",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


@staff_member_required
def protected_media(request, path):
    """Stream a staff-only media file without exposing arbitrary server paths."""
    media_root = Path(settings.MEDIA_ROOT).resolve()

    try:
        candidate = Path(safe_join(str(media_root), path)).resolve(strict=True)
        candidate.relative_to(media_root)
    except (OSError, RuntimeError, SuspiciousFileOperation, ValueError):
        raise Http404("File not found")

    if not candidate.is_file():
        raise Http404("File not found")

    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    try:
        file_handle = candidate.open("rb")
    except OSError:
        raise Http404("File not found")

    response = FileResponse(
        file_handle,
        as_attachment=content_type not in INLINE_CONTENT_TYPES,
        filename=candidate.name,
        content_type=content_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
