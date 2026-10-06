"""The independent exact-account boundary for Team Activity."""
from functools import wraps

from django.core.exceptions import PermissionDenied

BOSS_USERNAME = "Acoeur"


def is_boss_user(user):
    return bool(
        user.is_authenticated
        and user.is_active
        and user.get_username() == BOSS_USERNAME
    )


def boss_account_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_boss_user(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped

