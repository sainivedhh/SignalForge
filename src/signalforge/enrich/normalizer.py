"""Enrichment text normalization module.

Strips HTML tags, normalizes Unicode & whitespace, heuristically detects language,
and safely truncates excessive text length.
"""

import html
import re
from typing import Tuple

_LANG_PATTERNS = {
    "es": {
        "de", "la", "el", "en", "y", "a", "los", "se", "del", "las", "por", "un",
        "para", "con", "no", "una", "su", "al", "lo", "como", "mas", "pero", "sus",
        "es", "muy", "mi", "cuenta", "servicio", "gracias", "favor", "puedo", "lento",
    },
    "fr": {
        "de", "la", "le", "et", "les", "des", "en", "un", "du", "une", "que", "est",
        "pour", "pas", "qui", "dans", "sur", "avec", "ce", "merci", "tres",
    },
    "de": {
        "der", "die", "und", "in", "den", "von", "zu", "das", "mit", "sich", "des",
        "ist", "nicht", "ein", "eine", "fuer", "auf", "dem", "dass", "sehr", "danke",
    },
    "hi_hinglish": {
        "nahi", "kya", "hai", "tha", "raha", "bhi", "bahut", "achha", "kharab",
        "mera", "aapka", "karo", "aur", "ye", "yeh", "kaise", "hua",
    },
}


def strip_html(text: str) -> str:
    """Unescape HTML entities and strip markup tags."""
    if not text:
        return ""
    unescaped = html.unescape(text)
    # Strip <script> or <style> tags and content
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", unescaped, flags=re.DOTALL | re.IGNORECASE)
    # Strip general HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return cleaned


def normalize_whitespace(text: str) -> str:
    """Normalize tabs, newlines, multiple spaces, and trim ends."""
    return re.sub(r"\s+", " ", text).strip()


def detect_language(text: str) -> str:
    """Heuristic language detection without external network or heavy model dependencies."""
    tokens = set(re.findall(r"\b[a-zA-Z]{2,}\b", text.lower()))
    if not tokens:
        return "en"

    best_lang = "en"
    best_score = 0

    for lang, keywords in _LANG_PATTERNS.items():
        overlap = len(tokens & keywords)
        if overlap > best_score:
            best_score = overlap
            best_lang = lang

    # If minimal overlap, default to en
    return best_lang if best_score >= 2 else "en"


def normalize_text(text: str, max_chars: int = 5000) -> Tuple[str, str, int]:
    """Complete normalization pipeline for raw text.

    Returns:
        (cleaned_text, detected_language, original_length)
    """
    orig_length = len(text)
    cleaned = strip_html(text)
    cleaned = normalize_whitespace(cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "..."
    lang = detect_language(cleaned)
    return cleaned, lang, orig_length
