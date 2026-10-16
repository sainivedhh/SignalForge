"""Ingest Lambda Handler.

Receives POST /feedback (single) or POST /feedback/batch (up to 100 items).
Validates payload with Pydantic, derives deterministic feedback_id,
writes messages to SQS, and returns 202 Accepted with request id.
Never blocks on AI work or processor execution.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List

import boto3
from pydantic import ValidationError
from signalforge.models import (
    FeedbackBatchInput,
    FeedbackInput,
    IngestMessage,
    compute_feedback_id,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_sqs_client = None


def get_sqs_client() -> Any:
    """Lazy initialize boto3 SQS client to reuse across invocations."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def make_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Format standardized API Gateway HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
        },
        "body": json.dumps(body),
    }


def send_sqs_batch(queue_url: str, messages: List[IngestMessage]) -> None:
    """Send up to 10 messages per SQS batch call (SQS limit is 10 items/batch)."""
    client = get_sqs_client()
    for i in range(0, len(messages), 10):
        chunk = messages[i : i + 10]
        entries = [
            {
                "Id": f"msg_{idx}_{m.feedback_id[:8]}",
                "MessageBody": m.model_dump_json(),
                "MessageAttributes": {
                    "source": {"DataType": "String", "StringValue": m.source},
                    "feedback_id": {"DataType": "String", "StringValue": m.feedback_id},
                },
            }
            for idx, m in enumerate(chunk)
        ]
        response = client.send_message_batch(QueueUrl=queue_url, Entries=entries)
        if response.get("Failed"):
            failures = response["Failed"]
            logger.error("SQS send_message_batch partial failure: %s", failures)
            raise RuntimeError(f"Failed to enqueue {len(failures)} messages to SQS")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """API Gateway Proxy Handler for Ingestion."""
    queue_url = os.environ.get("FEEDBACK_QUEUE_URL", "")
    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))

    if not event or "body" not in event or not event["body"]:
        return make_response(
            400,
            {
                "error": "BAD_REQUEST",
                "message": "Empty or missing request body",
                "request_id": request_id,
            },
        )

    try:
        raw_body = json.loads(event["body"])
    except json.JSONDecodeError as e:
        return make_response(
            400,
            {
                "error": "INVALID_JSON",
                "message": f"Malformed JSON: {str(e)}",
                "request_id": request_id,
            },
        )

    path = event.get("path", "")
    resource = event.get("resource", "")
    is_batch = "/batch" in path or "/batch" in resource or isinstance(raw_body, dict) and "items" in raw_body

    ingest_items: List[FeedbackInput] = []

    try:
        if is_batch:
            parsed_batch = FeedbackBatchInput.model_validate(raw_body)
            ingest_items = parsed_batch.items
        else:
            parsed_single = FeedbackInput.model_validate(raw_body)
            ingest_items = [parsed_single]
    except ValidationError as err:
        return make_response(
            422,
            {
                "error": "VALIDATION_ERROR",
                "message": "Input validation failed",
                "details": err.errors(include_url=False),
                "request_id": request_id,
            },
        )

    messages_to_queue: List[IngestMessage] = []
    feedback_ids: List[str] = []

    for item in ingest_items:
        fb_id = compute_feedback_id(
            source=item.source,
            external_id=item.external_id,
            text=item.text,
        )
        feedback_ids.append(fb_id)
        msg = IngestMessage(
            feedback_id=fb_id,
            source=item.source,
            text=item.text,
            customer_id=item.customer_id,
            timestamp=item.timestamp.isoformat(),
            external_id=item.external_id,
            metadata=item.metadata,
        )
        messages_to_queue.append(msg)

    # Deliver to SQS
    if queue_url:
        try:
            send_sqs_batch(queue_url, messages_to_queue)
        except Exception as e:
            logger.exception("Failed sending messages to SQS: %s", e)
            return make_response(
                500,
                {
                    "error": "INTERNAL_ERROR",
                    "message": "Failed to enqueue feedback for processing",
                    "request_id": request_id,
                },
            )
    else:
        logger.warning("FEEDBACK_QUEUE_URL not configured. SQS send skipped.")

    return make_response(
        202,
        {
            "status": "ACCEPTED",
            "request_id": request_id,
            "count": len(feedback_ids),
            "feedback_ids": feedback_ids,
        },
    )
