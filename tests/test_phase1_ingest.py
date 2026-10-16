"""Unit and moto tests for Phase 1: Ingestion, Idempotency, DLQ, and Processor."""

import json

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError
from signalforge.handlers.ingest import handler as ingest_handler
from signalforge.handlers.processor import handler as processor_handler
from signalforge.models import FeedbackInput, compute_feedback_id

from scripts.redrive_dlq import redrive_dlq


def test_compute_feedback_id_deterministic():
    # Same source and external_id yields same hash
    id1 = compute_feedback_id("zendesk", "TICKET-1234", "The app crashed today")
    id2 = compute_feedback_id("zendesk", "TICKET-1234", "Different text same external id")
    assert id1 == id2
    assert len(id1) == 32

    # Without external_id, hashes text content
    id3 = compute_feedback_id("twitter", None, "The app crashed today")
    id4 = compute_feedback_id("twitter", None, "The app crashed today")
    id5 = compute_feedback_id("twitter", None, "Other text")
    assert id3 == id4
    assert id3 != id5


def test_feedback_input_validation():
    # Valid input
    fb = FeedbackInput(
        source="Zendesk",
        text="Cannot log in to my account since yesterday",
        customer_id="cust_001",
    )
    assert fb.source == "zendesk"

    # Blank text error
    with pytest.raises(ValidationError):
        FeedbackInput(
            source="Zendesk",
            text="   ",
            customer_id="cust_001",
        )

    # Empty source error
    with pytest.raises(ValidationError):
        FeedbackInput(
            source="",
            text="Valid text here",
            customer_id="cust_001",
        )


@mock_aws
def test_ingest_handler_single_and_batch(monkeypatch):
    sqs = boto3.client("sqs", region_name="us-east-1")
    q = sqs.create_queue(QueueName="feedback-queue")
    queue_url = q["QueueUrl"]
    monkeypatch.setenv("FEEDBACK_QUEUE_URL", queue_url)

    # 1. Single valid ingestion
    event = {
        "path": "/feedback",
        "body": json.dumps({
            "source": "Zendesk",
            "text": "App is failing to sync files on mobile",
            "customer_id": "cust_456",
            "external_id": "TICKET-99",
        }),
    }
    res = ingest_handler(event, None)
    assert res["statusCode"] == 202
    body = json.loads(res["body"])
    assert body["status"] == "ACCEPTED"
    assert body["count"] == 1
    assert len(body["feedback_ids"]) == 1

    # Verify message in SQS
    sqs_res = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
    messages = sqs_res.get("Messages", [])
    assert len(messages) == 1
    msg_data = json.loads(messages[0]["Body"])
    assert msg_data["source"] == "zendesk"
    assert msg_data["external_id"] == "TICKET-99"

    # 2. Batch ingestion
    batch_event = {
        "path": "/feedback/batch",
        "body": json.dumps({
            "items": [
                {"source": "Email", "text": "Billing issue refund", "customer_id": "c1"},
                {"source": "Twitter", "text": "Love the new dark mode!", "customer_id": "c2"},
            ]
        }),
    }
    res_batch = ingest_handler(batch_event, None)
    assert res_batch["statusCode"] == 202
    b_body = json.loads(res_batch["body"])
    assert b_body["count"] == 2


@mock_aws
def test_processor_idempotency_and_partial_batch(monkeypatch):
    # Setup DynamoDB
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table_name = "TestFeedbackTable"
    table = dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "feedback_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "feedback_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("FEEDBACK_TABLE_NAME", table_name)

    # Create batch with 1 valid message, 1 poison message (malformed JSON)
    valid_payload = {
        "feedback_id": "fb_11111111111111111111111111111111",
        "source": "zendesk",
        "text": "Cannot connect to server 500 error",
        "customer_id": "cust_123",
        "timestamp": "2026-10-16T12:00:00Z",
    }
    event = {
        "Records": [
            {
                "messageId": "msg-valid-1",
                "body": json.dumps(valid_payload),
            },
            {
                "messageId": "msg-poison-2",
                "body": "THIS IS NOT JSON {{{",
            },
        ]
    }

    # Execute processor
    result = processor_handler(event, None)
    # Poison message reported as failure, valid message succeeded
    assert len(result["batchItemFailures"]) == 1
    assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-poison-2"

    # Check DynamoDB has the valid record
    item = table.get_item(Key={"feedback_id": valid_payload["feedback_id"]}).get("Item")
    assert item is not None
    assert item["feedback_id"] == valid_payload["feedback_id"]
    assert item["sentiment"] in ["positive", "neutral", "negative"]

    # Test idempotency: re-deliver the exact same message
    dup_event = {
        "Records": [
            {
                "messageId": "msg-dup-1",
                "body": json.dumps(valid_payload),
            }
        ]
    }
    dup_result = processor_handler(dup_event, None)
    # Should succeed without failure
    assert len(dup_result["batchItemFailures"]) == 0


@mock_aws
def test_redrive_dlq_script():
    sqs = boto3.client("sqs", region_name="us-east-1")
    dlq = sqs.create_queue(QueueName="test-dlq")["QueueUrl"]
    main_q = sqs.create_queue(QueueName="test-main")["QueueUrl"]

    # Send 3 messages to DLQ
    for i in range(3):
        sqs.send_message(QueueUrl=dlq, MessageBody=json.dumps({"test_id": i}))

    # Redrive
    count = redrive_dlq(dlq, main_q, max_messages=10)
    assert count == 3

    # Main queue now has 3 messages
    main_msgs = sqs.receive_message(QueueUrl=main_q, MaxNumberOfMessages=10).get("Messages", [])
    assert len(main_msgs) == 3

    # DLQ is now empty
    dlq_msgs = sqs.receive_message(QueueUrl=dlq, MaxNumberOfMessages=10).get("Messages", [])
    assert len(dlq_msgs) == 0
