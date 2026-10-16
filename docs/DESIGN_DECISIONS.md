# Architectural & Engineering Design Decisions

This document details the engineering rationales behind the key design decisions in SignalForge, along with an architectural scaling blueprint for operation at **100x scale**.

---

## 1. Asynchronous Ingestion via SQS vs Synchronous AI Processing

### Decision
The ingestion API (`POST /feedback`) validates the payload and writes directly to an SQS queue, immediately returning HTTP `202 Accepted` with a deterministic `feedback_id`. It never blocks on LLM or enrichment execution.

### Rationale
1. **Unpredictable AI Latencies**: LLM inference times range from 800ms to 5,000ms+ per call. Running this synchronously inside an API Gateway request risks client timeouts (API Gateway 29-second hard limit), degrades user experience, and causes connection pool exhaustion on mobile clients.
2. **Traffic Surge Buffering**: Ingestion traffic often arrives in bursts (e.g. following a product release or marketing campaign). SQS absorbs surges gracefully without overloading downstream databases or LLM rate limits.
3. **Decoupled Failure Domains**: An outage or throttling in the enrichment model does not impact the ingestion API.

---

## 2. Partial Batch Responses (`ReportBatchItemFailures`)

### Decision
The Processor Lambda uses AWS Lambda's native `ReportBatchItemFailures` event source mapping response.

### Rationale
By default, if an AWS Lambda function processing an SQS batch raises an uncaught exception, SQS considers the **entire batch** to have failed. All 10 messages become visible again and are re-delivered. If 1 message is a corrupt "poison pill", the other 9 innocent messages would be re-processed repeatedly until reaching `maxReceiveCount`, wasting compute, increasing database IOPS, and risking duplicate side effects. With `ReportBatchItemFailures`, the processor returns only the specific failed `itemIdentifier`, allowing SQS to acknowledge the successful messages and retry only the failed item.

---

## 3. Idempotency & At-Least-Once Delivery Handling

### Decision
1. Deterministic `feedback_id` derivation: `SHA-256(source + ":" + external_id)` if `external_id` exists, or `SHA-256(source + ":" + content_hash)`.
2. Storage with DynamoDB conditional write: `attribute_not_exists(feedback_id)`.

### Rationale
SQS guarantees **at-least-once** delivery. Network blips, consumer timeouts, or concurrent worker handoffs can cause the exact same message to be delivered more than once. By deriving a deterministic ID and enforcing `attribute_not_exists(feedback_id)`, subsequent deliveries encounter `ConditionalCheckFailedException`, which the processor safely catches and treats as an idempotent no-op.

---

## 4. DynamoDB Key Schema & Sparse GSIs

### Decision
- Base Table: `feedback_id` (HASH).
- Global Secondary Indexes: `SourceDateIndex`, `SentimentDateIndex`, `ThemeDateIndex` with `timestamp` as sort key.
- Sparse Indexing: Attributes that are `None` (such as unassigned `theme_id`) are stripped before writing.

### Rationale
DynamoDB only creates index entries for items where the index partition and sort keys exist. By omitting `None` attributes, `ThemeDateIndex` remains a sparse index, reducing storage cost and speeding up index scans.

---

## 5. Resilience Strategy: Circuit Breaker & Fallback

### Decision
Wrap external provider calls in a stateful `CircuitBreaker` (3 failures threshold, 10s cooldown) with an offline `RuleBasedProvider` fallback.

### Rationale
When third-party APIs suffer high error rates or latency degradation, continuing to send requests exhausts worker threads waiting for timeouts. The circuit breaker trips to `OPEN`, immediately diverting subsequent items to the local rule-based fallback without wasting HTTP timeout cycles.

---

## 6. Blueprint for 100x Scale (From 1k/day to 10M/day)

At 100x scale (~120 writes/second sustained, 1,500/second peak), the following architectural changes would be introduced:

1. **GSI Write Sharding (Salting)**:
   - *Problem*: Low-cardinality partition keys like `sentiment="negative"` will hit the 1,000 WCU / partition physical limit on DynamoDB storage nodes.
   - *Solution*: Append a deterministic or random suffix to the GSI partition key: e.g. `negative#0` to `negative#9`. Reads query all 10 shards in parallel using Scatter-Gather fan-out.
2. **Kinesis Data Streams / SQS FIFO**:
   - For workloads requiring strict per-customer chronological ordering, transition to Amazon Kinesis Data Streams partitioned by `customer_id`.
3. **ElastiCache (Redis) for Content Caching & De-duplication**:
   - Transition the in-memory cache to a distributed Redis cluster so cache hits are shared across all Lambda execution contexts.
4. **DynamoDB Time-to-Live (TTL) & S3 Cold Storage**:
   - Enable TTL to auto-expire raw feedback records after 90 days, with DynamoDB Streams archiving cold data to an S3 Data Lake for querying via Amazon Athena.
