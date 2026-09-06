import re
from typing import Optional


_NUMBER_MARKER = r"(?:N\s*[°º]|N[O0]\.?|N\.?|NUM(?:E|É)RO|#)"
_NUMBER_VALUE = (
    r"(?P<number>"
    r"(?:[0-9]{4,10}|[0-9]{1,3}(?:[ \t]+[0-9]{3}){1,3})"
    r")"
)

_LABELED_ORDER_NUMBER_RE = re.compile(
    rf"\b(?:BON\s+DE\s+COMMANDE|ORDER)\s*:?[ \t\r\n]*"
    rf"(?:{_NUMBER_MARKER}[ \t\r\n]*)?"
    rf"{_NUMBER_VALUE}\b",
    re.IGNORECASE,
)

_STANDALONE_ORDER_NUMBER_RE = re.compile(
    rf"^[ \t\r\n]*(?:{_NUMBER_MARKER}[ \t\r\n]*)?"
    rf"{_NUMBER_VALUE}[ \t\r\n]*$",
    re.IGNORECASE,
)


def extract_bon_de_commande_from_text(text: str) -> Optional[str]:
    """Extract a labeled or standalone 4-10 digit order number safely."""
    value = str(text or "")

    for pattern in (
        _LABELED_ORDER_NUMBER_RE,
        _STANDALONE_ORDER_NUMBER_RE,
    ):
        match = pattern.search(value)
        if not match:
            continue

        digits = re.sub(r"\D", "", match.group("number"))
        if 4 <= len(digits) <= 10:
            return digits

    return None
