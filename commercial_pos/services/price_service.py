from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db.models import Q

from commercial_pos.models import CommercialPOPricePolicy


class CommercialPOError(ValueError):
    """Internal diagnostic only; never passed to the display context."""


MONEY_QUANT = Decimal("0.01")


def resolve_commercial_price(reference_date):
    if not isinstance(reference_date, date) or isinstance(reference_date, datetime):
        raise CommercialPOError("An actual shipment date is required.")
    policies = list(CommercialPOPricePolicy.objects.filter(is_active=True).filter(
        Q(start_date__isnull=True) | Q(start_date__lte=reference_date),
        Q(end_date__isnull=True) | Q(end_date__gte=reference_date),
    )[:2])
    if len(policies) != 1:
        raise CommercialPOError(f"Expected one commercial policy on {reference_date}; matched {len(policies)}.")
    policy = policies[0]
    try:
        price = Decimal(policy.unit_price)
        valid = price.is_finite() and price > 0 and price == price.quantize(MONEY_QUANT)
    except (InvalidOperation, TypeError, ValueError):
        valid = False
    if not valid or policy.currency != "EUR":
        raise CommercialPOError("Invalid commercial price or currency.")
    return policy, price
