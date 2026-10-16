"""Pydantic data models and schemas for SignalForge feedback pipeline."""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class FeedbackInput(BaseModel):
    """Raw feedback received at the ingestion API Gateway endpoint.

    Validates field presence, types, and enforces reasonable size limits
    to guard downstream serverless resources.
    """
    source: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Origin channel: e.g. Zendesk, Twitter, AppStore, Intercom, Email",
    )
    text: str = Field(
        ...,
        min_length=3,
        max_length=10000,
        description="Raw feedback text content. Max 10,000 characters.",
    )
    customer_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Identifier of customer or user submitting feedback.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO8601 timestamp when feedback occurred.",
    )
    external_id: Optional[str] = Field(
        default=None,
        max_length=256,
        description="External ticket/tweet/review ID from origin system if present.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional arbitrary metadata (device, app version, tier).",
    )

    @field_validator("source")
    @classmethod
    def clean_source(cls, v: str) -> str:
        s = v.strip().lower()
        if not s:
            raise ValueError("source cannot be blank")
        return s

    @field_validator("text")
    @classmethod
    def validate_text_not_empty(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 3:
            raise ValueError("text must contain at least 3 non-whitespace characters")
        return s


class FeedbackBatchInput(BaseModel):
    """Batch payload containing up to 100 feedback items."""
    items: List[FeedbackInput] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Batch of feedback items. Up to 100 items per request.",
    )


def compute_feedback_id(source: str, external_id: Optional[str], text: str) -> str:
    """Derive deterministic, idempotent feedback ID.

    If external_id is present: hash(source:external_id)
    Otherwise: hash(source:content_hash)

    Returns 32-character hexadecimal sha256 prefix.
    """
    cleaned_source = source.strip().lower()
    if external_id and external_id.strip():
        payload = f"{cleaned_source}:{external_id.strip()}"
    else:
        content_hash = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        payload = f"{cleaned_source}:{content_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class IngestMessage(BaseModel):
    """Message schema placed onto SQS by the Ingest Lambda."""
    feedback_id: str
    source: str
    text: str
    customer_id: str
    timestamp: str  # ISO string
    external_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    ingested_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class EnrichmentResult(BaseModel):
    """Structured output from LLM/Rule-based enrichment."""
    sentiment: Literal["positive", "neutral", "negative"]
    sentiment_score: float = Field(..., ge=-1.0, le=1.0)
    urgency: Literal["low", "medium", "high", "critical"]
    topic_tags: List[str] = Field(default_factory=list)
    summary: str = Field(..., max_length=300)
    fallback_used: bool = False
    cached: bool = False


class ProcessedFeedbackRecord(BaseModel):
    """Final enriched record stored into DynamoDB."""
    feedback_id: str
    source: str
    text_clean: str
    original_text_length: int
    customer_id: str
    timestamp: str
    external_id: Optional[str] = None
    language: str
    redaction_counts: Dict[str, int]
    sentiment: Literal["positive", "neutral", "negative"]
    sentiment_score: float
    urgency: Literal["low", "medium", "high", "critical"]
    topic_tags: List[str]
    summary: str
    theme_id: Optional[str] = None
    fallback_used: bool = False
    status: Literal["PROCESSED", "FAILED"] = "PROCESSED"
    created_at: str
    updated_at: str
