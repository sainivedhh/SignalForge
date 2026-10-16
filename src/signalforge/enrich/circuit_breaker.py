"""Circuit Breaker pattern for external enrichment providers.

States:
- CLOSED: Normal operation, requests flow to primary provider.
- OPEN: Provider failed K consecutive times. All calls immediately fall back.
- HALF_OPEN: Cooldown expired. Allows a trial call to test provider recovery.
"""

import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.state: str = "CLOSED"  # "CLOSED", "OPEN", "HALF_OPEN"
        self.consecutive_failures: int = 0
        self.last_failure_time: float = 0.0

    def can_execute(self) -> bool:
        """Check if request can be dispatched to provider."""
        now = time.time()
        if self.state == "OPEN":
            if now - self.last_failure_time >= self.cooldown_seconds:
                logger.info("Circuit breaker cooldown expired; switching to HALF_OPEN")
                self.state = "HALF_OPEN"
                return True
            return False
        return True

    def record_success(self) -> None:
        """Record successful invocation."""
        if self.state != "CLOSED":
            logger.info("Circuit breaker reset to CLOSED after successful invocation")
        self.state = "CLOSED"
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        """Record provider error."""
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        if self.consecutive_failures >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                "Circuit breaker tripped to OPEN (%d consecutive failures)",
                self.consecutive_failures,
            )
