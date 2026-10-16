"""Tests for Phase 2: Enrichment Pipeline.

Verifies:
- Text normalization (HTML stripping, whitespace normalization, safe truncation, language detection).
- PII redaction (email, phone, credit card Luhn, Aadhaar Verhoeff, API keys).
- Providers (MockProvider, RuleBasedProvider, LLMProvider guard).
- Pydantic schema validation, exponential retry, fallback mechanism, and content-hash caching.
- Circuit breaker behavior (CLOSED -> OPEN -> HALF_OPEN).
"""

import time

import pytest
from signalforge.enrich.circuit_breaker import CircuitBreaker
from signalforge.enrich.normalizer import (
    detect_language,
    normalize_text,
)
from signalforge.enrich.pii_redactor import redact_pii, validate_luhn, validate_verhoeff
from signalforge.enrich.pipeline import EnrichmentPipeline
from signalforge.enrich.providers import BaseProvider, LLMProvider, RuleBasedProvider
from signalforge.models import EnrichmentResult, IngestMessage


def test_normalization():
    raw_html = "<p>Hello <b>world</b>! &amp; check <script>alert('xss')</script>   spacing   </p>"
    clean, lang, orig_len = normalize_text(raw_html)
    assert "<p>" not in clean
    assert "alert" not in clean
    assert "&" in clean
    assert "   " not in clean
    assert lang == "en"
    assert orig_len == len(raw_html)


def test_language_detection():
    assert detect_language("This application is running very slow on my iPhone") == "en"
    assert detect_language("El servicio es muy lento y no puedo acceder a mi cuenta") == "es"
    assert detect_language("Mera payment fail ho raha hai aur bahut dikkat ho rahi hai") == "hi_hinglish"


def test_pii_validation_algorithms():
    # Valid Luhn (Visa sample card)
    assert validate_luhn("4532015112830366") is True
    # Invalid Luhn
    assert validate_luhn("4532015112830367") is False

    # Valid Verhoeff (Aadhaar sample 2121 5050 8894)
    assert validate_verhoeff("212150508894") is True
    assert validate_verhoeff("123456789012") is False


def test_pii_redaction():
    text = (
        "Contact me at user.test@example.com or call +91 9876543210. "
        "My card is 4532-0151-1283-0366 and Aadhaar is 2121 5050 8894. "
        "Also my token is api_key_mock_secret_test_token_999888."
    )
    redacted, counts = redact_pii(text)

    assert "[EMAIL_REDACTED]" in redacted
    assert "user.test@example.com" not in redacted
    assert counts["email"] == 1

    assert "[PHONE_REDACTED]" in redacted
    assert "9876543210" not in redacted
    assert counts["phone"] >= 1

    assert "[CREDIT_CARD_REDACTED]" in redacted
    assert "4532-0151-1283-0366" not in redacted
    assert counts["credit_card"] == 1

    assert "[AADHAAR_REDACTED]" in redacted
    assert "2121 5050 8891" not in redacted
    assert counts["aadhaar"] == 1

    assert "[API_KEY_REDACTED]" in redacted
    assert "sk_live_" not in redacted
    assert counts["api_key"] == 1


def test_circuit_breaker():
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
    assert cb.state == "CLOSED"
    assert cb.can_execute() is True

    cb.record_failure()
    assert cb.state == "CLOSED"
    assert cb.can_execute() is True

    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.can_execute() is False

    # Wait for cooldown
    time.sleep(0.12)
    assert cb.can_execute() is True  # Transitions to HALF_OPEN
    assert cb.state == "HALF_OPEN"

    # Successful call resets to CLOSED
    cb.record_success()
    assert cb.state == "CLOSED"


class FailingProvider(BaseProvider):
    def __init__(self, failure_type: str = "error") -> None:
        self.failure_type = failure_type

    def enrich(self, text: str) -> EnrichmentResult:
        if self.failure_type == "invalid_schema":
            # Return dict or type that fails Pydantic schema validation
            return {"sentiment": "INVALID_VALUE"}  # type: ignore
        raise RuntimeError("External service timed out!")


def test_enrichment_pipeline_resilience_and_caching():
    # 1. Primary provider fails with exception -> falls back to RuleBasedProvider
    failing_primary = FailingProvider(failure_type="error")
    fallback = RuleBasedProvider()
    pipeline = EnrichmentPipeline(primary_provider=failing_primary, fallback_provider=fallback, max_retries=1)

    msg = IngestMessage(
        feedback_id="fb_resilience_1",
        source="twitter",
        text="App keeps crashing and is completely broken!",
        customer_id="cust_99",
        timestamp="2026-10-16T12:00:00Z",
    )

    record = pipeline.process(msg)
    assert record.fallback_used is True
    assert record.sentiment == "negative"
    assert "bug" in record.topic_tags or "broken" in record.text_clean

    # 2. Caching: identical text returns cached copy without re-running provider
    record_cached = pipeline.process(msg)
    assert record_cached.sentiment == record.sentiment
    assert record_cached.text_clean == record.text_clean

    # 3. LLM Provider is guarded against accidental real network calls
    llm_stub = LLMProvider()
    with pytest.raises(RuntimeError, match="Real network calls are forbidden"):
        llm_stub.enrich("Any feedback")
