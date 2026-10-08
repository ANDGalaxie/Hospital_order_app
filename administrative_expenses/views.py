import logging
import mimetypes

from django.contrib import messages
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from hospital_engagements.boss_access import boss_account_required
from .categories import CATEGORIES, SUBCATEGORIES, category_label, validate_category
from .forms import AttachmentForm, ExpenseForm
from .models import AdministrativeExpense, AdministrativeExpenseAttachment
from .services import expenses, month_bounds, monthly_expenses, monthly_summary, remove_attachment, save_expense, summarize
from .storage import NAME_PATTERN, private_storage

logger = logging.getLogger(__name__)


def active_attachments(expense):
    return expense.attachments.filter(removed_at__isnull=True)


@boss_account_required
@require_safe
def home(request):
    try:
        context = monthly_summary(request.GET.get("month"))
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    return render(request, "administrative_expenses/home.html", context)


@boss_account_required
@require_safe
def category(request, category):
    if category not in CATEGORIES:
        raise Http404
    if category not in SUBCATEGORIES:
        return expense_list(request, category, "")
    try:
        context = monthly_summary(request.GET.get("month"))
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    context.update(category=category, title=CATEGORIES[category],
                   children=next(row["children"] for row in context["categories"] if row["code"] == category))
    return render(request, "administrative_expenses/category.html", context)


@boss_account_required
@require_safe
def expense_list(request, category, subcategory=""):
    try:
        validate_category(category, subcategory)
    except ValidationError:
        raise Http404
    try:
        queryset, start = monthly_expenses(request.GET.get("month"))
    except ValidationError as exc:
        return HttpResponseBadRequest(" ".join(exc.messages))
    queryset = queryset.filter(category=category, subcategory=subcategory)
    summary = summarize(queryset)
    summary["missing"] = queryset.filter(attachment_count=0).count()
    payment = request.GET.get("payment", "")
    document = request.GET.get("document", "")
    if payment not in {"", "pending", "paid"} or document not in {"", "missing", "attached"}:
        return HttpResponseBadRequest(_("Invalid filter."))
    if payment:
        queryset = queryset.filter(payment_status=payment)
    if document:
        queryset = queryset.filter(attachment_count=0) if document == "missing" else queryset.filter(attachment_count__gt=0)
    void = request.GET.get("void", "") == "1"
    if void:
        # Voided records are available for audit, never included in monthly totals.
        start, end = month_bounds(request.GET.get("month"))
        queryset = expenses().filter(is_void=True, category=category, subcategory=subcategory,
                                    expense_date__gte=start, expense_date__lt=end)
        if payment:
            queryset = queryset.filter(payment_status=payment)
        if document:
            queryset = queryset.filter(attachment_count=0) if document == "missing" else queryset.filter(attachment_count__gt=0)
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "administrative_expenses/list.html", {
        "page_obj": Paginator(queryset, 30).get_page(request.GET.get("page")),
        "title": category_label(category, subcategory), "category": category, "subcategory": subcategory,
        "month": start.strftime("%Y-%m"), "summary": summary,
        "filtered_summary": summarize(queryset) if not void else None,
        "payment": payment, "document": document, "show_void": void, "page_query": query.urlencode(),
    })


@boss_account_required
@require_safe
def detail(request, expense_id):
    expense = get_object_or_404(expenses(), pk=expense_id)
    return render(request, "administrative_expenses/detail.html", {
        "expense": expense, "attachments": active_attachments(expense), "attachment_form": AttachmentForm(),
    })


@boss_account_required
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def edit(request, expense_id=None):
    expense = get_object_or_404(AdministrativeExpense, pk=expense_id) if expense_id else None
    if expense and expense.is_void:
        return redirect("portal:administrative_expenses:detail", expense_id=expense.pk)
    initial = {"expense_date": timezone.localdate()}
    if not expense:
        initial.update(category=request.GET.get("category", ""), subcategory=request.GET.get("subcategory", ""))
    form = ExpenseForm(request.POST if request.method == "POST" else None, instance=expense, initial=initial)
    attachment_form = AttachmentForm(request.POST if request.method == "POST" else None, request.FILES if request.method == "POST" else None)
    if request.method == "POST":
        valid = form.is_valid()
        files_valid = attachment_form.is_valid()
        if valid and files_valid:
            try:
                expense = save_expense(form=form, expense_id=expense_id, user=request.user,
                    uploads=attachment_form.cleaned_data["files"], document_type=attachment_form.cleaned_data["document_type"])
            except ValidationError as exc:
                form.add_error(None, " ".join(exc.messages))
            except (OSError, DatabaseError, SuspiciousFileOperation):
                logger.exception("Administrative expense save failed")
                form.add_error(None, _("Unable to save. Existing records and attachments have been preserved; please try again."))
            else:
                messages.success(request, _("Expense saved."))
                return redirect("portal:administrative_expenses:detail", expense_id=expense.pk)
    return render(request, "administrative_expenses/form.html", {
        "form": form, "attachment_form": attachment_form, "expense": expense,
        "attachments": active_attachments(expense) if expense else [],
        "subcategory_map": {category: [(code, str(label)) for code, label in children.items()] for category, children in SUBCATEGORIES.items()},
    })


@boss_account_required
@require_POST
@csrf_protect
def upload(request, expense_id):
    expense = get_object_or_404(AdministrativeExpense, pk=expense_id, is_void=False)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid() and form.cleaned_data["files"]:
        try:
            save_expense(expense_id=expense.pk, user=request.user, uploads=form.cleaned_data["files"],
                         document_type=form.cleaned_data["document_type"])
        except (OSError, DatabaseError, ValidationError, SuspiciousFileOperation):
            logger.exception("Administrative attachment upload failed")
            form.add_error(None, _("Unable to upload attachments; please try again."))
        else:
            messages.success(request, _("Attachments uploaded."))
            return redirect("portal:administrative_expenses:detail", expense_id=expense.pk)
    elif form.is_valid():
        form.add_error("files", _("Choose at least one file."))
    return render(request, "administrative_expenses/detail.html", {
        "expense": expense, "attachments": active_attachments(expense), "attachment_form": form,
    }, status=400)


@boss_account_required
@require_POST
@csrf_protect
def remove(request, expense_id, attachment_id):
    get_object_or_404(AdministrativeExpenseAttachment, pk=attachment_id, expense_id=expense_id, removed_at__isnull=True)
    try:
        complete = remove_attachment(expense_id, attachment_id)
    except (OSError, DatabaseError, SuspiciousFileOperation, ValidationError):
        logger.exception("Administrative attachment removal failed")
        messages.error(request, _("Unable to remove attachment; please try again."))
    else:
        if complete:
            messages.success(request, _("Attachment removed."))
        else:
            messages.warning(request, _("Attachment removed from this expense. Private storage cleanup is pending."))
    return redirect("portal:administrative_expenses:detail", expense_id=expense_id)


@boss_account_required
@require_POST
@csrf_protect
def void(request, expense_id):
    with transaction.atomic():
        expense = get_object_or_404(AdministrativeExpense.objects.select_for_update(), pk=expense_id)
        expense.is_void = True
        expense.updated_by = request.user
        expense.save(update_fields=["is_void", "updated_by", "updated_at"])
    messages.success(request, _("Expense voided."))
    return redirect("portal:administrative_expenses:detail", expense_id=expense.pk)


@boss_account_required
@require_safe
def download(request, expense_id, attachment_id, preview=False):
    attachment = get_object_or_404(AdministrativeExpenseAttachment, pk=attachment_id,
                                  expense_id=expense_id, removed_at__isnull=True)
    name = attachment.file.name
    if not NAME_PATTERN.fullmatch(name) or name.split("/")[1] != str(expense_id):
        raise Http404
    try:
        handle = private_storage.open(name, "rb")
    except (OSError, ValueError, SuspiciousFileOperation):
        raise Http404
    response = FileResponse(handle, as_attachment=not preview, filename=attachment.original_filename,
                            content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response
