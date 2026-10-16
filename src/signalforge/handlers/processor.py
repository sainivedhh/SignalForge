"""Processor Lambda Handler.

Consumes SQS batches with partial batch failure responses (ReportBatchItemFailures).
Applies normalization, PII redaction, enrichment, and idempotently puts records
into DynamoDB using conditional expressions to guarantee at-least-once safety.
"""

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError
from signalforge.enrich.pipeline import EnrichmentPipeline
from signalforge.models import IngestMessage, ProcessedFeedbackRecord

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb_resource = None
_pipeline = None


def get_dynamodb_resource() -> Any:
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def get_pipeline() -> EnrichmentPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = EnrichmentPipeline()
    return _pipeline


def clean_dynamo_item(obj: Any) -> Any:
    """Recursively clean dict to convert float to Decimal and strip None/Null values for sparse GSI keys."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: clean_dynamo_item(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [clean_dynamo_item(v) for v in obj if v is not None]
    return obj


def store_feedback_idempotent(table: Any, record: ProcessedFeedbackRecord) -> bool:
    """Store record into DynamoDB.

    Uses conditional put: attribute_not_exists(feedback_id).
    If record already exists, does nothing and returns False (idempotent no-op).
    Returns True if newly inserted.
    """
    item = clean_dynamo_item(record.model_dump())
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(feedback_id)",
        )
        return True

    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "Feedback ID %s already processed (idempotent duplicate delivery)",
                record.feedback_id,
            )
            return False
        logger.error("DynamoDB put_item failed for %s: %s", record.feedback_id, e)
        raise


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """SQS Batch Consumer Handler.

    Returns ReportBatchItemFailures format:
    {"batchItemFailures": [{"itemIdentifier": messageId}]}
    """
    table_name = os.environ.get("FEEDBACK_TABLE_NAME", "SignalForgeFeedback")
    dynamo = get_dynamodb_resource()
    table = dynamo.Table(table_name)
    pipeline = get_pipeline()

    records = event.get("Records", [])
    batch_item_failures: List[Dict[str, str]] = []

    for record in records:
        msg_id = record.get("messageId", "")
        body_raw = record.get("body", "")

        try:
            # Step 1: Parse message body
            try:
                body_json = json.loads(body_raw)
            except json.JSONDecodeError as err:
                # Poison message: invalid JSON cannot be fixed by retrying.
                # In SQS, if we fail it repeatedly it goes to DLQ.
                logger.error("Malformed JSON in message %s: %s", msg_id, err)
                batch_item_failures.append({"itemIdentifier": msg_id})
                continue

            try:
                msg = IngestMessage.model_validate(body_json)
            except ValidationError as err:
                logger.error("Validation error for message %s: %s", msg_id, err)
                batch_item_failures.append({"itemIdentifier": msg_id})
                continue

            # Step 2: Enrichment (Normalization, PII redaction, Tagging, Summary)
            enriched_record = pipeline.process(msg)

            # Step 3: DynamoDB Conditional Put (Idempotency)
            store_feedback_idempotent(table, enriched_record)

        except Exception as e:
            logger.exception("Unexpected error processing message %s: %s", msg_id, e)
            batch_item_failures.append({"itemIdentifier": msg_id})

    return {"batchItemFailures": batch_item_failures}
