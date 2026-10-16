"""Full enrichment pipeline orchestrator.

Coordinates:
1. Normalization (strip HTML, normalize spaces, detect language, safe truncate).
2. PII Redaction (strip emails, cards [Luhn], Aadhaar [Verhoeff], phones, API keys).
3. In-memory content-hash caching so identical text is not re-processed.
4. Circuit-breaker guarded provider dispatch with retry and exponential backoff.
5. Automatic fallback to RuleBasedProvider on provider failure or invalid schema.
"""

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from pydantic import ValidationError
from signalforge.enrich.circuit_breaker import CircuitBreaker
from signalforge.enrich.normalizer import normalize_text
from signalforge.enrich.pii_redactor import redact_pii
from signalforge.enrich.providers import BaseProvider, RuleBasedProvider
from signalforge.models import (
    EnrichmentResult,
    IngestMessage,
    ProcessedFeedbackRecord,
)

logger = logging.getLogger(__name__)


class EnrichmentPipeline:
    def __init__(
        self,
        primary_provider: Optional[BaseProvider] = None,
        fallback_provider: Optional[BaseProvider] = None,
        max_retries: int = 2,
    ) -> None:
        self.primary_provider = primary_provider or RuleBasedProvider()
        self.fallback_provider = fallback_provider or RuleBasedProvider()
        self.circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10.0)
        self.max_retries = max_retries
        self._cache: Dict[str, EnrichmentResult] = {}

    def _get_content_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def enrich_with_resilience(self, redacted_text: str) -> EnrichmentResult:
        content_hash = self._get_content_hash(redacted_text)
        if content_hash in self._cache:
            cached_res = self._cache[content_hash]
            return cached_res.model_copy(update={"cached": True})

        result: Optional[EnrichmentResult] = None
        fallback_needed = False

        if self.circuit_breaker.can_execute():
            # Try primary provider with retries
            for attempt in range(self.max_retries + 1):
                try:
                    res = self.primary_provider.enrich(redacted_text)
                    # Validate against pydantic schema
                    validated = EnrichmentResult.model_validate(res)
                    self.circuit_breaker.record_success()
                    result = validated
                    break
                except (ValidationError, Exception) as e:
                    logger.warning("Primary provider attempt %d failed: %s", attempt + 1, e)
                    if attempt < self.max_retries:
                        time.sleep(0.01 * (2 ** attempt))
                    else:
                        self.circuit_breaker.record_failure()
                        fallback_needed = True
        else:
            logger.info("Circuit breaker OPEN; immediately routing to fallback provider")
            fallback_needed = True

        if fallback_needed or result is None:
            # Fall back to rule based provider
            try:
                fallback_res = self.fallback_provider.enrich(redacted_text)
                result = fallback_res.model_copy(update={"fallback_used": True})
            except Exception as e:
                logger.error("Fallback provider failed: %s", e)
                result = EnrichmentResult(
                    sentiment="neutral",
                    sentiment_score=0.0,
                    urgency="medium",
                    topic_tags=["general"],
                    summary=redacted_text[:120],
                    fallback_used=True,
                )

        self._cache[content_hash] = result
        return result

    def process(self, msg: IngestMessage) -> ProcessedFeedbackRecord:
        """Execute full pipeline for an incoming IngestMessage."""
        # 1. Normalization
        clean_text, detected_lang, orig_length = normalize_text(msg.text)

        # 2. PII Redaction BEFORE enrichment
        redacted_text, redaction_counts = redact_pii(clean_text)

        # 3. Enrichment
        enrichment = self.enrich_with_resilience(redacted_text)

        now_iso = datetime.now(timezone.utc).isoformat()

        return ProcessedFeedbackRecord(
            feedback_id=msg.feedback_id,
            source=msg.source,
            text_clean=redacted_text,
            original_text_length=orig_length,
            customer_id=msg.customer_id,
            timestamp=msg.timestamp,
            external_id=msg.external_id,
            language=detected_lang,
            redaction_counts=redaction_counts,
            sentiment=enrichment.sentiment,
            sentiment_score=enrichment.sentiment_score,
            urgency=enrichment.urgency,
            topic_tags=enrichment.topic_tags,
            summary=enrichment.summary,
            theme_id=None,
            fallback_used=enrichment.fallback_used,
            status="PROCESSED",
            created_at=now_iso,
            updated_at=now_iso,
        )
