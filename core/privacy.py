"""Mask payment details a customer may type by mistake, before a ticket is stored or sent to the model.

Card numbers (13-19 digits, spaces or dashes allowed) keep their last four digits. One-time codes,
CVVs and PINs are masked when they follow those words. Phone numbers, order IDs and refund
references are left alone, because support needs them.
"""

from __future__ import annotations

import re

CARD_RE = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
CODE_RE = re.compile(r"\b(OTP|one[- ]time (?:password|code)|verification code|CVV|CVC|PIN)\b(\W{0,12}(?:is\W{1,3})?)(\d{3,8})\b",
                     re.IGNORECASE)


def _mask_card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group())
    return f"•••• •••• •••• {digits[-4:]}"


def mask_sensitive(text: str) -> str:
    if not text:
        return text
    text = CARD_RE.sub(_mask_card, text)
    return CODE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}••••", text)
