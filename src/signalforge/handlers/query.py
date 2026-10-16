"""Query Lambda Handler for SignalForge feedback retrieval API.

Endpoints:
- GET /feedback: Filter by source, sentiment, theme, date range with cursor pagination.
- GET /feedback/{id}: Single feedback lookup.
- GET /themes: Discovered themes with item counts.
- GET /stats/summary: High-level metric aggregation.
"""

import base64
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_dynamodb_resource = None


def get_dynamodb_resource() -> Any:
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def decimal_to_python(obj: Any) -> Any:
    """Recursively convert Decimal objects back to float/int for JSON serialization."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: decimal_to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_to_python(v) for v in obj]
    return obj


def encode_cursor(last_evaluated_key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode DynamoDB LastEvaluatedKey to opaque base64 string."""
    if not last_evaluated_key:
        return None
    raw_json = json.dumps(decimal_to_python(last_evaluated_key))
    return base64.urlsafe_b64encode(raw_json.encode("utf-8")).decode("utf-8")


def decode_cursor(cursor_str: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode base64 cursor back to DynamoDB ExclusiveStartKey."""
    if not cursor_str:
        return None
    try:
        decoded_json = base64.urlsafe_b64decode(cursor_str.encode("utf-8")).decode("utf-8")
        return json.loads(decoded_json)
    except Exception:
        logger.warning("Invalid cursor provided: %s", cursor_str)
        return None


def make_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        },
        "body": json.dumps(decimal_to_python(body)),
    }


def query_feedback(table: Any, params: Dict[str, str]) -> Dict[str, Any]:
    """Execute filtered query on appropriate GSI or table without unbounded scans."""
    source = params.get("source")
    sentiment = params.get("sentiment")
    theme_id = params.get("theme")
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    limit = min(int(params.get("limit", 20)), 100)
    cursor = params.get("cursor")

    exclusive_start_key = decode_cursor(cursor)

    query_kwargs: Dict[str, Any] = {"Limit": limit}
    if exclusive_start_key:
        query_kwargs["ExclusiveStartKey"] = exclusive_start_key

    # Determine optimal GSI based on index partition key
    if source:
        query_kwargs["IndexName"] = "SourceDateIndex"
        if start_date and end_date:
            query_kwargs["KeyConditionExpression"] = (
                Key("source").eq(source) & Key("timestamp").between(start_date, end_date)
            )
        else:
            query_kwargs["KeyConditionExpression"] = Key("source").eq(source)

        if sentiment:
            query_kwargs["FilterExpression"] = Attr("sentiment").eq(sentiment)

        res = table.query(**query_kwargs)

    elif sentiment:
        query_kwargs["IndexName"] = "SentimentDateIndex"
        if start_date and end_date:
            query_kwargs["KeyConditionExpression"] = (
                Key("sentiment").eq(sentiment) & Key("timestamp").between(start_date, end_date)
            )
        else:
            query_kwargs["KeyConditionExpression"] = Key("sentiment").eq(sentiment)

        res = table.query(**query_kwargs)

    elif theme_id:
        query_kwargs["IndexName"] = "ThemeDateIndex"
        if start_date and end_date:
            query_kwargs["KeyConditionExpression"] = (
                Key("theme_id").eq(theme_id) & Key("timestamp").between(start_date, end_date)
            )
        else:
            query_kwargs["KeyConditionExpression"] = Key("theme_id").eq(theme_id)

        res = table.query(**query_kwargs)

    else:
        # Default bounded pagination on table
        res = table.scan(**query_kwargs)

    items = res.get("Items", [])
    next_cursor = encode_cursor(res.get("LastEvaluatedKey"))

    return {
        "items": items,
        "count": len(items),
        "next_cursor": next_cursor,
    }


def get_feedback_by_id(table: Any, feedback_id: str) -> Optional[Dict[str, Any]]:
    """O(1) Primary Key lookup by feedback_id."""
    res = table.get_item(Key={"feedback_id": feedback_id})
    return res.get("Item")


def get_themes_summary(table: Any) -> Dict[str, Any]:
    """Retrieve list of themes with aggregated feedback counts."""
    # Bounded scan limited to 200 items for summary demonstration
    res = table.scan(Limit=200, ProjectionExpression="theme_id, sentiment")
    items = res.get("Items", [])

    themes: Dict[str, Dict[str, int]] = {}
    for item in items:
        t_id = item.get("theme_id") or "unassigned"
        s = item.get("sentiment", "neutral")
        if t_id not in themes:
            themes[t_id] = {"total": 0, "positive": 0, "neutral": 0, "negative": 0}
        themes[t_id]["total"] += 1
        if s in themes[t_id]:
            themes[t_id][s] += 1

    theme_list = [
        {"theme_id": t_id, "stats": stats}
        for t_id, stats in sorted(themes.items(), key=lambda x: x[1]["total"], reverse=True)
    ]
    return {"themes": theme_list}


def get_stats_summary(table: Any) -> Dict[str, Any]:
    """Aggregate total items and sentiment distribution."""
    res = table.scan(
        Limit=500,
        ProjectionExpression="sentiment, #src, urgency",
        ExpressionAttributeNames={"#src": "source"},
    )
    items = res.get("Items", [])

    sentiments = {"positive": 0, "neutral": 0, "negative": 0}
    sources: Dict[str, int] = {}
    urgencies = {"low": 0, "medium": 0, "high": 0, "critical": 0}

    for it in items:
        s = it.get("sentiment")
        if s in sentiments:
            sentiments[s] += 1
        src = it.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
        u = it.get("urgency")
        if u in urgencies:
            urgencies[u] += 1

    total = len(items)
    return {
        "total_feedback": total,
        "sentiment_breakdown": sentiments,
        "source_breakdown": sources,
        "urgency_breakdown": urgencies,
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Router for GET Query endpoints."""
    table_name = os.environ.get("FEEDBACK_TABLE_NAME", "SignalForgeFeedback")
    dynamo = get_dynamodb_resource()
    table = dynamo.Table(table_name)

    path = event.get("path", "")
    event.get("httpMethod", "GET")
    query_params = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    try:
        # Route 1: GET /feedback/{id}
        if "id" in path_params or (path.startswith("/feedback/") and len(path.split("/")) > 2):
            fb_id = path_params.get("id") or path.split("/")[-1]
            item = get_feedback_by_id(table, fb_id)
            if not item:
                return make_response(404, {"error": "NOT_FOUND", "message": f"Feedback {fb_id} not found"})
            return make_response(200, {"item": item})

        # Route 2: GET /themes
        if "/themes" in path:
            result = get_themes_summary(table)
            return make_response(200, result)

        # Route 3: GET /stats/summary
        if "/stats/summary" in path:
            result = get_stats_summary(table)
            return make_response(200, result)

        # Route 4: GET /feedback (Query with filters & pagination)
        if "/feedback" in path:
            result = query_feedback(table, query_params)
            return make_response(200, result)

        return make_response(404, {"error": "ROUTE_NOT_FOUND", "path": path})

    except Exception as e:
        logger.exception("Error executing query endpoint: %s", e)
        return make_response(500, {"error": "INTERNAL_SERVER_ERROR", "message": str(e)})
