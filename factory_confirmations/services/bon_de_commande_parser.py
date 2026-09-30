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
    rf"{_NUMBER_VALUE}\b(?P<suffix>[ \t]*-[ \t]*[bB][ \t]*[^\s,;]*)?",
    re.IGNORECASE,
)

_STANDALONE_ORDER_NUMBER_RE = re.compile(
    rf"^[ \t\r\n]*(?:{_NUMBER_MARKER}[ \t\r\n]*)?"
    rf"{_NUMBER_VALUE}(?P<suffix>[ \t]*-[ \t]*[bB][ \t]*[^\s,;]*)?[ \t\r\n]*$",
    re.IGNORECASE,
)


def parse_factory_order_reference(text: str) -> Optional[dict]:
    """Parse only labeled or standalone references; never merge suffix digits."""
    value = str(text or "")
    for pattern in (_LABELED_ORDER_NUMBER_RE, _STANDALONE_ORDER_NUMBER_RE):
        match = pattern.search(value)
        if not match:
            continue
        digits = re.sub(r"\D", "", match.group("number"))
        if not 4 <= len(digits) <= 10:
            continue
        suffix = match.group("suffix")
        batch_number = None
        if suffix is not None:
            suffix_match = re.fullmatch(r"[ \t]*-[ \t]*B[ \t]*([0-9]+)", suffix, re.I)
            if not suffix_match or int(suffix_match.group(1)) < 1:
                raise ValueError("工厂订单编号的批次后缀无效；必须为 -B<number>，且批次 >= 1。")
            batch_number = int(suffix_match.group(1))
        return {
            "raw_reference": digits + (f"-B{batch_number}" if suffix else ""),
            "bon_de_commande": digits,
            "batch_number": batch_number,
            "has_explicit_batch": suffix is not None,
        }
    return None


def extract_bon_de_commande_from_text(text: str) -> Optional[str]:
    """Compatibility API returning the base BON only."""
    reference = parse_factory_order_reference(text)
    return reference["bon_de_commande"] if reference else None
