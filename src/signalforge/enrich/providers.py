"""Pluggable providers for feedback enrichment.

Includes:
- BaseProvider: Abstract interface.
- MockProvider: Deterministic, offline mock provider for tests.
- RuleBasedProvider: High-performance lexicon and keyword baseline.
- LLMProvider: Production stub for structured LLM JSON outputs (calls real API only if enabled).
"""

import os
import re
from abc import ABC, abstractmethod
from typing import List, Literal

from signalforge.models import EnrichmentResult

# Configurable domain taxonomy
TAXONOMY_KEYWORDS = {
    "billing": [
        "bill", "charge", "refund", "receipt", "invoice", "overcharge", "pricing",
        "subscription", "credit card", "payment", "renewal", "discount", "fee",
    ],
    "performance": [
        "slow", "lag", "latency", "freeze", "crash", "timeout", "hanging", "spinning",
        "load", "sluggish", "unresponsive", "delay", "glitch",
    ],
    "bug": [
        "bug", "error", "exception", "broken", "fail", "failure", "not working",
        "doesn't work", "crash", "white screen", "404", "500", "stacktrace",
    ],
    "ui_ux": [
        "confusing", "ugly", "cluttered", "navigation", "button", "layout", "font",
        "color", "dark mode", "mobile view", "hard to find", "interface", "design",
    ],
    "account": [
        "login", "password", "2fa", "sso", "authentication", "sign in", "reset",
        "logout", "signup", "register", "session", "locked", "profile",
    ],
    "feature_request": [
        "would be great", "please add", "feature request", "can you add", "suggest",
        "missing", "wish list", "support for", "export to", "integration",
    ],
}

POSITIVE_LEXICON = {
    "love", "great", "excellent", "amazing", "awesome", "fantastic", "helpful",
    "fast", "smooth", "best", "brilliant", "superb", "thank", "pleased", "good",
    "easy", "reliable", "wonderful", "perfect",
}

NEGATIVE_LEXICON = {
    "terrible", "horrible", "awful", "worst", "hate", "useless", "broken",
    "angry", "frustrated", "disappointed", "annoying", "poor", "rubbish",
    "waste", "slow", "pain", "unacceptable", "scam", "sucks", "disaster",
    "ridiculous", "bad", "fail", "failing", "overcharge", "overcharged", "bug", "crash",
}

CRITICAL_KEYWORDS = {
    "data loss", "legal", "lawyer", "regulator", "security vulnerability",
    "gdpr", "breach", "urgent", "emergency", "production down", "critical",
}

HIGH_KEYWORDS = {
    "cannot work", "blocked", "immediate", "asap", "can't login", "cannot login",
    "lost access", "cancel account", "churn",
}


class BaseProvider(ABC):
    @abstractmethod
    def enrich(self, text: str) -> EnrichmentResult:
        """Enrich cleaned text into structured sentiment, urgency, tags, summary."""
        raise NotImplementedError


class MockProvider(BaseProvider):
    """Deterministic mock provider for testing and offline scenarios."""
    def __init__(self, forced_sentiment: str | None = None) -> None:
        self.forced_sentiment = forced_sentiment

    def enrich(self, text: str) -> EnrichmentResult:
        lower = text.lower()
        if self.forced_sentiment:
            sentiment: Literal["positive", "neutral", "negative"] = self.forced_sentiment  # type: ignore
        elif any(w in lower for w in ["love", "great", "good", "happy"]):
            sentiment = "positive"
        elif any(w in lower for w in ["bad", "terrible", "worst", "awful", "hate", "broken"]):
            sentiment = "negative"
        else:
            sentiment = "neutral"

        sentiment_score = 0.8 if sentiment == "positive" else (-0.8 if sentiment == "negative" else 0.0)
        summary = text[:120] + ("..." if len(text) > 120 else "")

        return EnrichmentResult(
            sentiment=sentiment,
            sentiment_score=sentiment_score,
            urgency="high" if "urgent" in lower or sentiment == "negative" else "low",
            topic_tags=["performance"] if "slow" in lower else ["general"],
            summary=summary,
            fallback_used=False,
        )


class RuleBasedProvider(BaseProvider):
    """Lexicon, negation-aware, and rule-based fallback provider.

    Acts as the baseline and guaranteed offline fallback engine.
    """
    def enrich(self, text: str) -> EnrichmentResult:
        tokens = re.findall(r"\b[a-zA-Z']+\b", text.lower())

        pos_count: float = 0.0
        neg_count: float = 0.0
        negation_active = False

        negators = {"not", "never", "no", "hardly", "barely", "scarcely", "without", "isn't", "aren't", "wasn't"}

        for _i, token in enumerate(tokens):
            if token in negators:
                negation_active = True
                continue

            # Negation flips next 2 tokens
            is_pos = token in POSITIVE_LEXICON
            is_neg = token in NEGATIVE_LEXICON

            if negation_active:
                if is_pos:
                    neg_count += 1.0
                elif is_neg:
                    pos_count += 0.5
                negation_active = False
            else:
                if is_pos:
                    pos_count += 1.0
                elif is_neg:
                    neg_count += 1.0

        total_sentiment_words = pos_count + neg_count
        if total_sentiment_words == 0:
            sentiment_score = 0.0
            sentiment: Literal["positive", "neutral", "negative"] = "neutral"
        else:
            raw_score = (pos_count - neg_count) / total_sentiment_words
            sentiment_score = round(max(-1.0, min(1.0, raw_score)), 2)
            if sentiment_score >= 0.2:
                sentiment = "positive"
            elif sentiment_score <= -0.2:
                sentiment = "negative"
            else:
                sentiment = "neutral"

        # Topic detection
        matched_tags: List[str] = []
        lower_text = text.lower()
        for topic, kws in TAXONOMY_KEYWORDS.items():
            for kw in kws:
                if re.search(r"\b" + re.escape(kw) + r"\b", lower_text):
                    matched_tags.append(topic)
                    break
        if not matched_tags:
            matched_tags = ["other"]

        # Urgency scoring
        if any(re.search(r"\b" + re.escape(kw) + r"\b", lower_text) for kw in CRITICAL_KEYWORDS):
            urgency: Literal["low", "medium", "high", "critical"] = "critical"
        elif any(re.search(r"\b" + re.escape(kw) + r"\b", lower_text) for kw in HIGH_KEYWORDS) or sentiment == "negative":
            urgency = "high"
        elif sentiment == "neutral":
            urgency = "medium"
        else:
            urgency = "low"

        # Summary generation: first informative sentence or truncate
        sentences = re.split(r"(?<=[.!?])\s+", text)
        first_sentence = sentences[0] if sentences else text
        if len(first_sentence) > 200:
            summary = first_sentence[:197] + "..."
        else:
            summary = first_sentence

        return EnrichmentResult(
            sentiment=sentiment,
            sentiment_score=sentiment_score,
            urgency=urgency,
            topic_tags=matched_tags,
            summary=summary,
            fallback_used=False,
        )


class LLMProvider(BaseProvider):
    """Stub LLM provider showing production structured JSON schema integration.

    Guarded so that it NEVER makes network calls unless explicitly enabled via
    ENABLE_REAL_LLM=true environment variable.
    """
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.enabled = os.environ.get("ENABLE_REAL_LLM", "false").lower() == "true"

    def enrich(self, text: str) -> EnrichmentResult:
        if not self.enabled:
            raise RuntimeError("LLMProvider is disabled. Real network calls are forbidden without ENABLE_REAL_LLM=true.")

        # In actual deployment, calls OpenAI / Anthropic / Amazon Bedrock here
        # and parses JSON into EnrichmentResult.
        raise NotImplementedError("Real LLM execution disabled in portfolio offline mode.")
