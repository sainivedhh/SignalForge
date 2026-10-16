# DynamoDB Access Patterns and Schema Design

This document describes the access patterns, partition key designs, Global Secondary Indexes (GSIs), and architectural trade-offs for the SignalForge customer feedback intelligence store.

---

## 1. Access Patterns Summary

| # | Access Pattern | Target Query / Action | Key Schema / Index | Query Method |
|---|----------------|-----------------------|--------------------|--------------|
| **AP1** | Ingest feedback item idempotently | Write single feedback item, fail if duplicate | Primary Table: `PK=feedback_id` | `PutItem` with `ConditionExpression="attribute_not_exists(feedback_id)"` |
| **AP2** | Fetch single feedback item by ID | `GET /feedback/{id}` | Primary Table: `PK=feedback_id` | `GetItem(Key={"feedback_id": id})` |
| **AP3** | Query feedback by Source & Date range | `GET /feedback?source={src}&start_date={...}&end_date={...}` | `GSI1 (SourceDateIndex)`: `PK=source`, `SK=timestamp` | `Query` on GSI1 with `KeyConditionExpression="source = :s AND #ts BETWEEN :t1 AND :t2"` |
| **AP4** | Query feedback by Sentiment & Date range | `GET /feedback?sentiment={pos\|neu\|neg}` | `GSI2 (SentimentDateIndex)`: `PK=sentiment`, `SK=timestamp` | `Query` on GSI2 with `KeyConditionExpression="sentiment = :s AND #ts BETWEEN :t1 AND :t2"` |
| **AP5** | Query feedback by Theme ID & Date range | `GET /feedback?theme={theme_id}` | `GSI3 (ThemeDateIndex)`: `PK=theme_id`, `SK=timestamp` | `Query` on GSI3 with `KeyConditionExpression="theme_id = :t AND #ts BETWEEN :t1 AND :t2"` |
| **AP6** | List all discovered themes | `GET /themes` | Aggregate query over themes partition or recent items | Query / Scan with projection |
| **AP7** | Aggregate feedback stats summary | `GET /stats/summary` | Query recent window on Sentiment / Source GSI | Filtered count aggregation |

---

## 2. Table Design & Primary Key Schema

### Base Table: `SignalForgeFeedback`
- **Partition Key (HASH)**: `feedback_id` (String, SHA-256 derived hex hash).
- **Attributes**:
  - `feedback_id`: String
  - `source`: String (e.g. `zendesk`, `twitter`, `email`)
  - `customer_id`: String
  - `timestamp`: String (ISO-8601 UTC)
  - `external_id`: Optional String
  - `text_clean`: String (Redacted & Normalized)
  - `language`: String (`en`, `es`, `hi_hinglish`, etc.)
  - `sentiment`: String (`positive`, `neutral`, `negative`)
  - `sentiment_score`: Number (Decimal)
  - `urgency`: String (`low`, `medium`, `high`, `critical`)
  - `topic_tags`: List of Strings
  - `theme_id`: Optional String
  - `redaction_counts`: Map
  - `summary`: String
  - `fallback_used`: Boolean
  - `created_at`: String (ISO-8601 UTC)

---

## 3. Global Secondary Indexes (GSIs)

### GSI1: `SourceDateIndex`
- **Partition Key**: `source` (String)
- **Sort Key**: `timestamp` (String)
- **Projection**: `ALL`
- **Purpose**: Enables filtering by feedback origin channel (e.g., Zendesk tickets vs Twitter mentions) ordered chronologically with cursor pagination (`ExclusiveStartKey`).

### GSI2: `SentimentDateIndex`
- **Partition Key**: `sentiment` (String)
- **Sort Key**: `timestamp` (String)
- **Projection**: `ALL`
- **Purpose**: Allows support and product teams to retrieve all negative feedback in the last 24 hours without scanning the entire database.

### GSI3: `ThemeDateIndex`
- **Partition Key**: `theme_id` (String)
- **Sort Key**: `timestamp` (String)
- **Projection**: `ALL`
- **Purpose**: Allows drilling down into feedback records associated with a specific clustered theme or anomaly spike.

---

## 4. Architectural Trade-offs & Mitigations

### 1. Hot Partitions on Low-Cardinality Keys
- **Risk**: `sentiment` has only 3 distinct values (`positive`, `neutral`, `negative`). High write throughput on `GSI2` could result in hot partition throttling on the storage partition for `negative`.
- **Mitigation & Trade-off**:
  - For baseline and internship-scale production (< 1,000 writes/sec), DynamoDB partition autoscaling easily handles uniform distribution across 3 partitions.
  - At **100x Scale**: We would use **write sharding (salt suffix)** on the GSI partition key: e.g. `negative#0` to `negative#9`, querying across the 10 shards in parallel using Scatter-Gather fan-out.

### 2. Cursor Pagination vs Scans
- **Rule**: No unbounded table scans in request paths.
- **Implementation**: All list queries accept a `limit` (default 20, max 100) and encode `LastEvaluatedKey` into a base64 opaque cursor token. The client passes `cursor=<token>` to resume pagination deterministically without scanning unread partitions.

### 3. Cost & Item Size Overhead
- **Design Decision**: Projecting `ALL` attributes onto GSIs avoids duplicate read calls back to the base table (`GetItem`), maximizing query speed (p95 < 15ms).
- **Trade-off**: Storage cost is multiplied per GSI. Because text is truncated to 5,000 characters and summaries are compact, the average item size remains ~1.5 KB, well below the 400 KB DynamoDB item limit.
