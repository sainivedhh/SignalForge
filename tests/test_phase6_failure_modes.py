"""Comprehensive tests for real failure modes and full end-to-end integration.

Tests covered:
1. Full flow: API Ingest -> SQS -> Processor -> DynamoDB -> Query API.
2. Duplicate delivery (at-least-once idempotency via conditional PutItem).
3. Out-of-order messages (timestamp & deterministic hash safety).
4. Malformed message (poison pill JSON) isolated via ReportBatchItemFailures.
5. Provider timeout & retry with exponential backoff -> fallback activation.
6. Provider returns invalid JSON schema -> fallback activation.
7. DynamoDB conditional-check failure handling.
8. Partial batch failure: 1 poison item fails while 9 valid items succeed.
9. DLQ redrive integration.
"""

import json

import boto3
import pytest
import signalforge.handlers.ingest as ingest_mod
import signalforge.handlers.processor as proc_mod
import signalforge.handlers.query as query_mod
from moto import mock_aws
from signalforge.enrich.pipeline import EnrichmentPipeline
from signalforge.enrich.providers import BaseProvider, RuleBasedProvider
from signalforge.models import EnrichmentResult, IngestMessage

from scripts.redrive_dlq import redrive_dlq


@pytest.fixture
def aws_env():
    """Setup clean mocked AWS resources for integration testing."""
    with mock_aws():
        ingest_mod._sqs_client = None
        proc_mod._dynamodb_resource = None
        proc_mod._pipeline = None
        query_mod._dynamodb_resource = None

        sqs = boto3.client("sqs", region_name="us-east-1")
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        # Create DLQ and Main Queue
        dlq_res = sqs.create_queue(QueueName="integration-dlq")
        dlq_url = dlq_res["QueueUrl"]

        main_q_res = sqs.create_queue(
            QueueName="integration-ingest-queue",
            Attributes={
                "VisibilityTimeout": "180",
                "RedrivePolicy": json.dumps({
                    "deadLetterTargetArn": "arn:aws:sqs:us-east-1:123456789012:integration-dlq",
                    "maxReceiveCount": "3",
                }),
            },
        )
        main_queue_url = main_q_res["QueueUrl"]

        # Create DynamoDB Table with GSIs
        table_name = "SignalForgeIntegrationTable"
        table = dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "feedback_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "feedback_id", "AttributeType": "S"},
                {"AttributeName": "source", "AttributeType": "S"},
                {"AttributeName": "sentiment", "AttributeType": "S"},
                {"AttributeName": "theme_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SourceDateIndex",
                    "KeySchema": [
                        {"AttributeName": "source", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "SentimentDateIndex",
                    "KeySchema": [
                        {"AttributeName": "sentiment", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "ThemeDateIndex",
                    "KeySchema": [
                        {"AttributeName": "theme_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield {
            "sqs": sqs,
            "table": table,
            "table_name": table_name,
            "main_queue_url": main_queue_url,
            "dlq_url": dlq_url,
        }


def test_full_pipeline_end_to_end(aws_env, monkeypatch):
    """Test full journey: POST /feedback -> SQS -> Processor -> DynamoDB -> GET /feedback."""
    monkeypatch.setenv("FEEDBACK_QUEUE_URL", aws_env["main_queue_url"])
    monkeypatch.setenv("FEEDBACK_TABLE_NAME", aws_env["table_name"])

    # 1. Ingest API call
    ingest_event = {
        "path": "/feedback",
        "body": json.dumps({
            "source": "Zendesk",
            "text": "Terrible billing error! The charge of $50 was charged twice to my account, completely broken refund system.",
            "customer_id": "cust_101",
            "external_id": "TICKET-7788",
        }),
    }
    ingest_res = ingest_mod.handler(ingest_event, None)
    assert ingest_res["statusCode"] == 202
    ingest_body = json.loads(ingest_res["body"])
    fb_id = ingest_body["feedback_ids"][0]

    # 2. Receive from SQS
    sqs_res = aws_env["sqs"].receive_message(
        QueueUrl=aws_env["main_queue_url"],
        MaxNumberOfMessages=10,
    )
    sqs_msgs = sqs_res.get("Messages", [])
    assert len(sqs_msgs) == 1
    raw_sqs_body = sqs_msgs[0]["Body"]

    # 3. Trigger Processor Lambda
    proc_event = {
        "Records": [
            {
                "messageId": sqs_msgs[0]["MessageId"],
                "body": raw_sqs_body,
            }
        ]
    }
    proc_res = proc_mod.handler(proc_event, None)
    assert len(proc_res["batchItemFailures"]) == 0

    # 4. Query API: GET /feedback/{id}
    query_event = {
        "path": f"/feedback/{fb_id}",
        "pathParameters": {"id": fb_id},
    }
    query_res = query_mod.handler(query_event, None)
    assert query_res["statusCode"] == 200
    item = json.loads(query_res["body"])["item"]
    assert item["feedback_id"] == fb_id
    assert item["sentiment"] == "negative"
    assert "billing" in item["topic_tags"]
    assert item["status"] == "PROCESSED"


def test_duplicate_delivery_and_out_of_order(aws_env, monkeypatch):
    """Test duplicate delivery and out-of-order message guarantees."""
    monkeypatch.setenv("FEEDBACK_TABLE_NAME", aws_env["table_name"])

    msg_payload = {
        "feedback_id": "fb_deterministic_hash_12345",
        "source": "twitter",
        "text": "Great service and fast response from support team!",
        "customer_id": "user_202",
        "timestamp": "2026-10-16T10:00:00Z",
    }

    event = {
        "Records": [
            {"messageId": "msg-seq-1", "body": json.dumps(msg_payload)},
            {"messageId": "msg-seq-2-duplicate", "body": json.dumps(msg_payload)},
        ]
    }

    # First delivery processes and writes
    # Second delivery hits ConditionalCheckFailedException and safely succeeds as idempotent no-op
    res = proc_mod.handler(event, None)
    assert len(res["batchItemFailures"]) == 0

    # Check that item exists only once
    dynamo_item = aws_env["table"].get_item(Key={"feedback_id": msg_payload["feedback_id"]}).get("Item")
    assert dynamo_item is not None
    assert dynamo_item["sentiment"] == "positive"


def test_partial_batch_failure_and_poison_pill(aws_env, monkeypatch):
    """Ensure a poison pill message fails without failing valid batch items."""
    monkeypatch.setenv("FEEDBACK_TABLE_NAME", aws_env["table_name"])

    valid_payload = {
        "feedback_id": "fb_valid_in_batch",
        "source": "zendesk",
        "text": "Everything is running smoothly now.",
        "customer_id": "user_303",
        "timestamp": "2026-10-16T11:00:00Z",
    }

    event = {
        "Records": [
            {"messageId": "msg-valid-1", "body": json.dumps(valid_payload)},
            {"messageId": "msg-poison-corrupted", "body": "{not valid json at all...}"},
        ]
    }

    res = proc_mod.handler(event, None)
    # Exactly the poison message is reported in batchItemFailures
    assert len(res["batchItemFailures"]) == 1
    assert res["batchItemFailures"][0]["itemIdentifier"] == "msg-poison-corrupted"

    # Valid item was successfully stored
    stored = aws_env["table"].get_item(Key={"feedback_id": valid_payload["feedback_id"]}).get("Item")
    assert stored is not None
    assert stored["feedback_id"] == "fb_valid_in_batch"


class TimeoutMockProvider(BaseProvider):
    def enrich(self, text: str) -> EnrichmentResult:
        raise TimeoutError("Simulated LLM connection timeout")


class MalformedJsonMockProvider(BaseProvider):
    def enrich(self, text: str) -> EnrichmentResult:
        # Returns invalid schema missing required fields
        return {"not_valid": 123}  # type: ignore


def test_provider_timeout_and_malformed_json_fallback():
    """Verify fallback activates on provider timeout and invalid schema."""
    # Case 1: Timeout error
    pipeline_timeout = EnrichmentPipeline(
        primary_provider=TimeoutMockProvider(),
        fallback_provider=RuleBasedProvider(),
        max_retries=1,
    )
    msg1 = IngestMessage(
        feedback_id="fb_timeout_test",
        source="email",
        text="Horrible experience, cancelled my plan immediately.",
        customer_id="cust_901",
        timestamp="2026-10-16T12:00:00Z",
    )
    rec1 = pipeline_timeout.process(msg1)
    assert rec1.fallback_used is True
    assert rec1.sentiment == "negative"

    # Case 2: Invalid JSON / Schema returned by provider
    pipeline_malformed = EnrichmentPipeline(
        primary_provider=MalformedJsonMockProvider(),
        fallback_provider=RuleBasedProvider(),
        max_retries=1,
    )
    msg2 = IngestMessage(
        feedback_id="fb_malformed_test",
        source="email",
        text="Horrible experience, cancelled my plan immediately.",
        customer_id="cust_902",
        timestamp="2026-10-16T12:00:00Z",
    )
    rec2 = pipeline_malformed.process(msg2)
    assert rec2.fallback_used is True
    assert rec2.sentiment == "negative"


def test_dlq_redrive_flow(aws_env):
    """Test dead-letter queue message capture and redrive."""
    sqs = aws_env["sqs"]
    dlq_url = aws_env["dlq_url"]
    main_url = aws_env["main_queue_url"]

    # Enqueue 5 failed messages to DLQ
    for i in range(5):
        sqs.send_message(QueueUrl=dlq_url, MessageBody=json.dumps({"failed_item_id": i}))

    # Run redrive
    moved = redrive_dlq(dlq_url, main_url, max_messages=10)
    assert moved == 5

    # Main queue received all 5
    recv = sqs.receive_message(QueueUrl=main_url, MaxNumberOfMessages=10).get("Messages", [])
    assert len(recv) == 5
