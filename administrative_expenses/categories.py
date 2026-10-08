from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

CATEGORIES = {
    "salary": _("Employee salaries"),
    "benefits": _("Employee benefits"),
    "office_supplies": _("Office supplies"),
    "premises": _("Company premises"),
    "reimbursement": _("Reimbursements"),
}
SUBCATEGORIES = {
    "benefits": {
        "bonus": _("Bonuses"), "meals": _("Meals"),
        "transport": _("Transport allowance"), "commission": _("Commissions"),
        "housing": _("Housing allowance"), "holiday_gifts": _("Holiday gifts"),
    },
    "reimbursement": {
        "travel": _("Travel"), "entertainment": _("Entertainment"), "gifts": _("Gifts"),
    },
}


def validate_category(category, subcategory):
    if category not in CATEGORIES:
        raise ValidationError({"category": _("Invalid category.")})
    allowed = SUBCATEGORIES.get(category, {})
    if (allowed and subcategory not in allowed) or (not allowed and subcategory):
        raise ValidationError({"subcategory": _("Choose a valid subcategory for this category.")})


def category_label(category, subcategory=""):
    label = str(CATEGORIES.get(category, category))
    if subcategory:
        label += " / " + str(SUBCATEGORIES.get(category, {}).get(subcategory, subcategory))
    return label
