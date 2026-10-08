"""Expense mutations, Decimal summaries and recoverable private file lifecycle."""
import fcntl
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .categories import CATEGORIES, SUBCATEGORIES, category_label
from .models import AdministrativeExpense, AdministrativeExpenseAttachment, attachment_path
from .storage import NAME_PATTERN, private_storage
from .validation import validate_uploads

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")


@contextmanager
def journal_directory(create=False):
    """Keep interrupted upload metadata private, outside database rollback."""
    descriptor = os.open(settings.MEDIA_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ("administrative_expenses", "_uploads"):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


class UploadJournal:
    def __init__(self, names):
        self.name = uuid4().hex + ".json"
        with journal_directory(create=True) as directory:
            self.descriptor = os.open(self.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        # Recovery skips files held by live upload requests.
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        os.write(self.descriptor, json.dumps(names).encode("utf-8"))
        os.fsync(self.descriptor)

    def discard(self):
        with journal_directory() as directory:
            os.unlink(self.name, dir_fd=directory)

    def close(self):
        os.close(self.descriptor)


def expenses():
    return AdministrativeExpense.objects.annotate(
        attachment_count=Count("attachments", filter=Q(attachments__removed_at__isnull=True)),
    ).order_by("-expense_date", "-pk")


def month_bounds(value=None):
    if value:
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
                raise ValueError
            start = date.fromisoformat(value + "-01")
        except (TypeError, ValueError) as exc:
            raise ValidationError(_("Choose a valid month (YYYY-MM).")) from exc
    else:
        start = timezone.localdate().replace(day=1)
    if start.year == 9999 and start.month == 12:
        raise ValidationError(_("Choose a valid month (YYYY-MM)."))
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def monthly_expenses(month=None):
    start, end = month_bounds(month)
    return expenses().filter(is_void=False, expense_date__gte=start, expense_date__lt=end), start


def summarize(queryset):
    result = queryset.aggregate(
        total=Sum("amount"), paid=Sum("amount", filter=Q(payment_status="paid")),
        pending=Sum("amount", filter=Q(payment_status="pending")), count=Count("pk"),
    )
    return {key: (value if value is not None else ZERO) for key, value in result.items()}


def monthly_summary(month=None):
    queryset, start = monthly_expenses(month)
    summary = summarize(queryset)
    summary["missing"] = queryset.filter(attachment_count=0).count()
    # Aggregate expense rows without the attachment join, so multiple files never multiply amounts.
    rows = list(AdministrativeExpense.objects.filter(pk__in=queryset.values("pk"))
                .order_by().values("category", "subcategory")
                .annotate(amount_total=Sum("amount"), records=Count("pk")))
    categories = []
    for code, label in CATEGORIES.items():
        children = []
        for subcode, sublabel in SUBCATEGORIES.get(code, {}).items():
            matches = [row for row in rows if row["category"] == code and row["subcategory"] == subcode]
            children.append({"code": subcode, "label": sublabel, "total": sum((row["amount_total"] for row in matches), ZERO), "count": sum(row["records"] for row in matches)})
        matches = [row for row in rows if row["category"] == code]
        categories.append({"code": code, "label": label, "children": children,
                           "total": sum((row["amount_total"] for row in matches), ZERO),
                           "count": sum(row["records"] for row in matches)})
    return {"month": start.strftime("%Y-%m"), "summary": summary, "categories": categories,
            "recent": queryset[:10]}


def save_expense(*, form=None, expense_id=None, user, uploads, document_type):
    """All-or-nothing DB changes; rollback cleanup has a durable recovery journal."""
    validate_uploads(uploads)
    if document_type not in AdministrativeExpenseAttachment.DocumentType.values:
        raise ValidationError(_("Invalid document type."))
    journal = None
    written_names = []
    try:
        with transaction.atomic(durable=True):
            if expense_id:
                expense = AdministrativeExpense.objects.select_for_update().get(pk=expense_id)
                if expense.is_void:
                    raise ValidationError(_("Voided expenses cannot be edited."))
            else:
                expense = AdministrativeExpense(created_by=user)
            if form:
                for field in form.Meta.fields:
                    setattr(expense, field, form.cleaned_data[field])
                expense.updated_by = user
                expense.full_clean()
                expense.save()
            elif not expense_id:
                raise ValueError("An expense is required.")
            attachments = [
                AdministrativeExpenseAttachment(expense=expense, document_type=document_type,
                    original_filename=upload.name, file_size=upload.size, uploaded_by=user)
                for upload in uploads
            ]
            written_names = [attachment_path(attachment, upload.name) for attachment, upload in zip(attachments, uploads)]
            if written_names:
                journal = UploadJournal(written_names)
            for attachment, upload, name in zip(attachments, uploads, written_names):
                upload.seek(0)
                saved_name = private_storage.save(name, upload)
                if saved_name != name:
                    raise OSError("Unexpected private storage filename.")
                attachment.file = name
                attachment.full_clean()
                attachment.save()
    except Exception:
        clean = True
        for name in written_names:
            # A rejected UUID collision must never unlink a pre-existing attachment.
            if AdministrativeExpenseAttachment.objects.filter(file=name).exists():
                continue
            try:
                private_storage.delete(name)
            except OSError:
                clean = False
                logger.exception("Private attachment cleanup retained in upload journal")
        if journal:
            try:
                if clean:
                    journal.discard()
            except OSError:
                logger.exception("Upload journal retained for recovery")
            finally:
                journal.close()
        raise
    else:
        if journal:
            try:
                journal.discard()
            except OSError:
                logger.exception("Committed upload journal retained; recovery will preserve active files")
            finally:
                journal.close()
        return expense


def remove_attachment(expense_id, attachment_id):
    # Only unlink after committing the tombstone. Failed deletes remain managed and inaccessible.
    with transaction.atomic(durable=True):
        AdministrativeExpense.objects.select_for_update().get(pk=expense_id)
        attachment = AdministrativeExpenseAttachment.objects.select_for_update().get(
            pk=attachment_id, expense_id=expense_id, removed_at__isnull=True,
        )
        if (not NAME_PATTERN.fullmatch(attachment.file.name)
                or attachment.file.name.split("/")[1] != str(expense_id)):
            raise ValidationError(_("Invalid attachment path."))
        attachment.removed_at = timezone.now()
        attachment.save(update_fields=["removed_at"])
    try:
        private_storage.delete(attachment.file.name)
        attachment.delete()
        return True
    except OSError:
        logger.exception("Attachment removal retained for retry")
        return False


def recover_files():
    """Explicit maintenance only: no GET request runs recovery or deletes files."""
    recovered = 0
    for attachment in AdministrativeExpenseAttachment.objects.filter(removed_at__isnull=False):
        if (not NAME_PATTERN.fullmatch(attachment.file.name)
                or attachment.file.name.split("/")[1] != str(attachment.expense_id)):
            raise ValueError("Invalid attachment ownership; refusing file removal.")
        private_storage.delete(attachment.file.name)
        attachment.delete()
        recovered += 1
    try:
        with journal_directory() as directory:
            for name in os.listdir(directory):
                if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
                    continue
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    with os.fdopen(os.dup(descriptor), "rb") as handle:
                        paths = json.load(handle)
                    if not isinstance(paths, list) or not all(isinstance(path, str) and NAME_PATTERN.fullmatch(path) for path in paths):
                        raise ValueError("Invalid upload recovery journal.")
                    for path in paths:
                        if not AdministrativeExpenseAttachment.objects.filter(file=path, removed_at__isnull=True).exists():
                            private_storage.delete(path)
                            recovered += 1
                    os.unlink(name, dir_fd=directory)
                finally:
                    os.close(descriptor)
    except FileNotFoundError:
        pass
    return recovered
