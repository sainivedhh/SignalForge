"""PII Redaction module for customer feedback.

Redacts emails, phone numbers (Indian & international), credit cards (validated via Luhn),
Aadhaar numbers (validated via Verhoeff), and API keys/tokens BEFORE any text goes to an LLM.
Returns redacted text and redaction counters. Raw PII is never retained or logged.
"""

import re
from typing import Dict, Tuple

# Verhoeff algorithm multiplication table
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

# Verhoeff permutation table
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def validate_luhn(card_number_str: str) -> bool:
    """Validate numeric string using standard Luhn (mod 10) algorithm."""
    digits = [int(c) for c in card_number_str if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for i, digit in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = digit * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += digit
    return checksum % 10 == 0


def validate_verhoeff(num_str: str) -> bool:
    """Validate 12-digit Aadhaar number using Verhoeff checksum algorithm."""
    digits = [int(c) for c in num_str if c.isdigit()]
    if len(digits) != 12:
        return False
    c = 0
    for i, digit in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][digit]]
    return c == 0


# Common regex patterns for PII
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b")

# Indian & International phone numbers (+91 9876543210, 09876543210, +1-555-555-5555, etc.)
RE_PHONE = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b"
)

# API Keys, bearer tokens, AWS keys (AKIA...), JWTs, generic hex/base64 tokens
RE_API_KEY = re.compile(
    r"\b((?:sk_live|sk_test|api_key|ghp|bearer|xox[baprs]|AKIA|aws_secret)[A-Za-z0-9_\-=]{16,80}|[0-9a-fA-F]{32,64})\b",
    re.IGNORECASE,
)

# Candidate Credit Card patterns (13-19 digits, possibly hyphen or space separated)
RE_CARD_CANDIDATE = re.compile(r"\b(?:\d{4}[-\s]?){3,4}\d{1,4}\b|\b\d{13,19}\b")

# Candidate Aadhaar patterns (4 digits space/hyphen 4 digits space/hyphen 4 digits, or 12 continuous digits)
RE_AADHAAR_CANDIDATE = re.compile(r"\b\d{4}[-\s]\d{4}[-\s]\d{4}\b|\b\d{12}\b")


def redact_pii(text: str) -> Tuple[str, Dict[str, int]]:
    """Redact sensitive PII from feedback text.

    Returns:
        redacted_text: Text with sensitive items replaced by markers e.g. [EMAIL_REDACTED].
        counts: Dictionary recording counts of each redacted entity type.
    """
    counts = {
        "email": 0,
        "phone": 0,
        "credit_card": 0,
        "aadhaar": 0,
        "api_key": 0,
    }

    # 1. API Keys & Secrets
    def _replace_api_key(match: re.Match) -> str:
        counts["api_key"] += 1
        return "[API_KEY_REDACTED]"

    text = RE_API_KEY.sub(_replace_api_key, text)

    # 2. Email Addresses
    def _replace_email(match: re.Match) -> str:
        counts["email"] += 1
        return "[EMAIL_REDACTED]"

    text = RE_EMAIL.sub(_replace_email, text)

    # 3. Credit Cards (strict Luhn validation to prevent false positives)
    def _replace_card(match: re.Match) -> str:
        raw = match.group(0)
        digits_only = re.sub(r"\D", "", raw)
        if validate_luhn(digits_only):
            counts["credit_card"] += 1
            return "[CREDIT_CARD_REDACTED]"
        return raw

    text = RE_CARD_CANDIDATE.sub(_replace_card, text)

    # 4. Aadhaar Numbers (strict Verhoeff algorithm validation)
    def _replace_aadhaar(match: re.Match) -> str:
        raw = match.group(0)
        digits_only = re.sub(r"\D", "", raw)
        # Avoid treating credit card matches as Aadhaar if length happens to be 12
        if len(digits_only) == 12 and validate_verhoeff(digits_only):
            counts["aadhaar"] += 1
            return "[AADHAAR_REDACTED]"
        return raw

    text = RE_AADHAAR_CANDIDATE.sub(_replace_aadhaar, text)

    # 5. Phone Numbers (after Cards and Aadhaar so 12-digit/16-digit IDs are not matched as phone)
    def _replace_phone(match: re.Match) -> str:
        raw = match.group(0)
        # Verify it has enough digits to be an actual phone number (7-15 digits)
        digits_only = re.sub(r"\D", "", raw)
        if 7 <= len(digits_only) <= 15:
            counts["phone"] += 1
            return "[PHONE_REDACTED]"
        return raw

    text = RE_PHONE.sub(_replace_phone, text)

    return text, counts
