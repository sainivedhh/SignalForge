# SignalForge: Backend Software Engineering Interview Questions

Based on the actual codebase implementation (`src/signalforge/`), here are 15 technical interview questions with accurate answers.

---

### Q1: What happens if the Processor Lambda times out mid-batch?
**Answer**: In `template.yaml`, the SQS queue's `VisibilityTimeout` is configured to `180s`, while the Processor Lambda's `Timeout` is `30s`. If the Lambda execution times out or crashes mid-batch, any messages that were not acknowledged remain in flight. When the 180s visibility timeout expires, SQS makes them visible again for redelivery. Because our processor writes records using DynamoDB's `attribute_not_exists(feedback_id)` condition, messages that were already processed before the timeout will trigger a harmless `ConditionalCheckFailedException` (idempotent no-op), while unprocessed messages will be processed normally.

---

### Q2: How do you handle poison pill messages so they don't block the queue?
**Answer**: We configure two safety mechanisms:
1. **`ReportBatchItemFailures`**: When a malformed JSON message or schema validation failure occurs, the processor isolates it and returns `{"batchItemFailures": [{"itemIdentifier": msg_id}]}`. SQS acknowledges the valid sibling messages in the batch and only retries the bad message.
2. **Dead-Letter Queue (DLQ)**: The SQS queue specifies a `RedrivePolicy` with `maxReceiveCount: 3`. Once the poison message fails 3 times, SQS moves it to `FeedbackDLQ` and triggers a CloudWatch alarm.

---

### Q3: How is feedback ID derived, and why is it deterministic?
**Answer**: In `src/signalforge/models.py::compute_feedback_id`, if an `external_id` exists, we hash `f"{source}:{external_id}"` with SHA-256; if absent, we hash `f"{source}:{content_hash}"`. This guarantees that identical feedback delivered multiple times by webhook retries or network replays always derives the exact same 32-character key, enabling deterministic deduplication at the storage layer.

---

### Q4: How would you handle a hot partition on the `sentiment` Global Secondary Index at 100x scale?
**Answer**: `sentiment` has only 3 distinct values (`positive`, `neutral`, `negative`), which risks DynamoDB partition throttling under heavy write loads (>1,000 WCU). At 100x scale, we apply **write sharding (salting)**: appending an integer suffix e.g. `negative#0` through `negative#9` based on `hash(feedback_id) % 10`. To query negative feedback, the Query Lambda issues 10 parallel queries across the salted partitions and merges the results.

---

### Q5: Why is PII redaction performed before passing text to the LLM?
**Answer**: For compliance (GDPR, DPDP, PCI-DSS) and confidentiality. Passing raw customer emails, credit cards, or Aadhaar numbers to third-party AI APIs leaks user data into external logs and third-party models. Redacting first (`src/signalforge/enrich/pii_redactor.py`) ensures that neither external APIs nor internal logs ever retain raw customer PII.

---

### Q6: How do you avoid false positives when redacting credit card numbers and government IDs?
**Answer**: Rather than relying purely on regular expressions, candidate digit sequences undergo checksum verification:
- Credit card candidates are validated using the **Luhn (mod 10)** algorithm (`validate_luhn`).
- 12-digit Aadhaar candidates are validated using the **Verhoeff checksum** algorithm (`validate_verhoeff`).
Only candidates passing algorithmic checksums are redacted, preventing normal order IDs, serial numbers, or quantities from being redacted.

---

### Q7: Why do we use a circuit breaker for the enrichment provider?
**Answer**: If an upstream AI API goes down or experiences severe rate limiting, continuing to send requests ties up Lambda execution threads waiting for HTTP timeouts. The `CircuitBreaker` (`src/signalforge/enrich/circuit_breaker.py`) trips to `OPEN` after 3 consecutive failures. During the 10-second cooldown period, subsequent requests immediately route to `RuleBasedProvider` without wasting network latency.

---

### Q8: How does cursor-based pagination work in `GET /feedback`?
**Answer**: DynamoDB does not support offset-based pagination. When a query reaches its limit, DynamoDB returns a `LastEvaluatedKey`. In `src/signalforge/handlers/query.py`, we serialize this key into an opaque URL-safe base64 string (`next_cursor`). The client provides `?cursor=<token>`, which is decoded into `ExclusiveStartKey` to resume reading seamlessly without full table scans.

---

### Q9: Why does the SAM template specify `ReservedConcurrentExecutions` for each function?
**Answer**:
1. It prevents a sudden traffic surge on the ingestion API from consuming the entire AWS account's Lambda concurrency pool (default 1,000), which would starve other critical workloads.
2. It protects downstream DynamoDB tables from write throttling by capping processor concurrency at 20 instances.

---

### Q10: How do you ensure least privilege in IAM policies?
**Answer**: In `template.yaml`, functions are never granted wildcard (`*`) resource permissions. The Ingest Lambda is only granted `sqs:SendMessage` on the specific ingest queue ARN. The Processor Lambda is granted `dynamodb:PutItem` strictly on `FeedbackTable.Arn`, and the Query Lambda is granted read-only permissions (`GetItem`, `Query`, `Scan`) on the table and index ARNs.

---

### Q11: What is the purpose of the DLQ redrive script (`scripts/redrive_dlq.py`)?
**Answer**: When downstream bugs are fixed, messages in the dead-letter queue need to be reprocessed. The script reads batches from the DLQ, re-enqueues them to the main ingest queue, and deletes them from the DLQ only upon verified delivery, ensuring zero data loss during maintenance.

---

### Q12: Why do you store `Decimal` types in DynamoDB instead of standard Python `float`?
**Answer**: The Python `boto3` DynamoDB serializer explicitly rejects native Python `float` types to prevent floating-point precision loss. In `clean_dynamo_item`, we recursively convert all floats (like `sentiment_score: -0.8`) to `Decimal("-0.8")` prior to `put_item`.

---

### Q13: How does the theme clustering algorithm work without external network calls?
**Answer**: `ThemeClusterer` (`src/signalforge/analytics/theme_spike.py`) uses scikit-learn's `TfidfVectorizer` (sublinear term frequency, English stop words) to convert feedback into sparse vector representations, then applies `AgglomerativeClustering` with cosine distance. It extracts the top TF-IDF terms per cluster center to dynamically generate human-readable, explainable theme names.

---

### Q14: How does the spike detector prevent alert storms?
**Answer**: In `SpikeDetector`, whenever an alert is published to the SNS topic, we record the timestamp in an alert cooldown dictionary (`_last_alert_time`). Any subsequent surges for the same theme within the 300-second cooldown window are suppressed.

---

### Q15: Why is the LLM Provider implemented as a stub with an explicit environment flag guard?
**Answer**: For test hermeticity and cost safety. The `LLMProvider` showcases how production JSON schema generation is structured, but enforces `ENABLE_REAL_LLM=true` checks to ensure that test runs, CI pipelines, and local benchmarks never make accidental external network calls or incur API costs.
