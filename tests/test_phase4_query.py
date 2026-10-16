"""Tests for Phase 4: DynamoDB Query API, GSI filtering, and cursor pagination."""

import json
import os
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws
from signalforge.handlers.query import handler as query_handler


@pytest.fixture
def dynamodb_with_feedback():
    """Setup DynamoDB table with GSIs and test feedback data."""
    with mock_aws():
        import signalforge.handlers.query as qm
        qm._dynamodb_resource = None  # Reset client within mock context
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table_name = os.environ.get("FEEDBACK_TABLE_NAME", "TestFeedbackTable")

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

        # Seed 5 records
        items = [
            {
                "feedback_id": "fb_001",
                "source": "zendesk",
                "customer_id": "c1",
                "timestamp": "2026-10-01T10:00:00Z",
                "sentiment": "negative",
                "sentiment_score": Decimal("-0.8"),
                "urgency": "high",
                "theme_id": "theme_billing",
                "text_clean": "Overcharged on billing",
            },
            {
                "feedback_id": "fb_002",
                "source": "zendesk",
                "customer_id": "c2",
                "timestamp": "2026-10-02T10:00:00Z",
                "sentiment": "positive",
                "sentiment_score": Decimal("0.9"),
                "urgency": "low",
                "theme_id": "theme_ui",
                "text_clean": "Great new design",
            },
            {
                "feedback_id": "fb_003",
                "source": "twitter",
                "customer_id": "c3",
                "timestamp": "2026-10-03T10:00:00Z",
                "sentiment": "negative",
                "sentiment_score": Decimal("-0.7"),
                "urgency": "high",
                "theme_id": "theme_billing",
                "text_clean": "Subscription error",
            },
            {
                "feedback_id": "fb_004",
                "source": "twitter",
                "customer_id": "c4",
                "timestamp": "2026-10-04T10:00:00Z",
                "sentiment": "neutral",
                "sentiment_score": Decimal("0.0"),
                "urgency": "medium",
                "theme_id": "theme_ui",
                "text_clean": "Where is the logout button?",
            },
        ]
        for it in items:
            table.put_item(Item=it)

        yield table


def test_get_feedback_by_id(dynamodb_with_feedback):
    event = {
        "path": "/feedback/fb_001",
        "pathParameters": {"id": "fb_001"},
    }
    res = query_handler(event, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["item"]["feedback_id"] == "fb_001"
    assert body["item"]["source"] == "zendesk"

    # Non-existent ID returns 404
    event_404 = {
        "path": "/feedback/nonexistent_id",
        "pathParameters": {"id": "nonexistent_id"},
    }
    res_404 = query_handler(event_404, None)
    assert res_404["statusCode"] == 404


def test_query_by_source_gsi(dynamodb_with_feedback):
    event = {
        "path": "/feedback",
        "queryStringParameters": {
            "source": "zendesk",
            "start_date": "2026-10-01T00:00:00Z",
            "end_date": "2026-10-05T00:00:00Z",
        },
    }
    res = query_handler(event, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["count"] == 2
    for item in body["items"]:
        assert item["source"] == "zendesk"


def test_query_by_sentiment_gsi(dynamodb_with_feedback):
    event = {
        "path": "/feedback",
        "queryStringParameters": {
            "sentiment": "negative",
        },
    }
    res = query_handler(event, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["count"] == 2
    for item in body["items"]:
        assert item["sentiment"] == "negative"


def test_cursor_pagination(dynamodb_with_feedback):
    # Page 1: limit 2
    event_p1 = {
        "path": "/feedback",
        "queryStringParameters": {"limit": "2"},
    }
    res_p1 = query_handler(event_p1, None)
    assert res_p1["statusCode"] == 200
    body_p1 = json.loads(res_p1["body"])
    assert body_p1["count"] == 2
    cursor = body_p1["next_cursor"]
    assert cursor is not None

    # Page 2: with cursor
    event_p2 = {
        "path": "/feedback",
        "queryStringParameters": {"limit": "2", "cursor": cursor},
    }
    res_p2 = query_handler(event_p2, None)
    assert res_p2["statusCode"] == 200
    body_p2 = json.loads(res_p2["body"])
    assert body_p2["count"] == 2
    # Ensure distinct items across pages
    ids_p1 = {it["feedback_id"] for it in body_p1["items"]}
    ids_p2 = {it["feedback_id"] for it in body_p2["items"]}
    assert ids_p1.isdisjoint(ids_p2)


def test_stats_and_themes_summary(dynamodb_with_feedback):
    # Themes summary
    event_themes = {"path": "/themes"}
    res_themes = query_handler(event_themes, None)
    assert res_themes["statusCode"] == 200
    body_themes = json.loads(res_themes["body"])
    assert "themes" in body_themes
    assert len(body_themes["themes"]) >= 2

    # Stats summary
    event_stats = {"path": "/stats/summary"}
    res_stats = query_handler(event_stats, None)
    assert res_stats["statusCode"] == 200
    body_stats = json.loads(res_stats["body"])
    assert body_stats["total_feedback"] == 4
    assert body_stats["sentiment_breakdown"]["negative"] == 2
    assert body_stats["sentiment_breakdown"]["positive"] == 1
