# Local Concurrency Load Test Results

> **CRITICAL DISCLAIMER**: These numbers reflect **in-process local Python execution only** on development hardware. They measure the pure algorithmic overhead of normalization, PII regex and checksum validation, and lexicon rule-based enrichment. They do **not** represent production AWS end-to-end network latency across API Gateway, SQS queues, or DynamoDB writes.

---

## 1. Test Command & Benchmark Execution

```powershell
$env:PYTHONPATH="src;."
.\.venv\Scripts\python scripts/load_test.py --items 500 --concurrency 8
```

---

## 2. Test Environment & Machine Description

| Component | Specification |
|-----------|---------------|
| **Processor** | 13th Gen Intel(R) Core(TM) i5-1335U (10 Cores, 12 Logical Processors) |
| **Operating System** | Windows 11 Home (x86_64) |
| **Python Version** | Python 3.11.8 |
| **Concurrency Model** | In-process `ThreadPoolExecutor` (8 worker threads) |
| **Workload Size** | 500 synthetic customer feedback items |

---

## 3. Real Benchmark Results

### Overall Pipeline Throughput & Latency

| Metric | Real Measured Result |
|--------|----------------------|
| **Wall Clock Execution Duration** | `0.359s` |
| **Throughput** | **1,392.4 items / second** |
| **Total Latency (p50)** | **0.58 ms** |
| **Total Latency (p95)** | **1.28 ms** |
| **Total Latency (p99)** | **79.53 ms** |

---

## 4. Latency Breakdown by Pipeline Stage (p95)

```
[ Ingested Raw Text ]
        │
        ▼ (p95: 0.08 ms)
[ 1. Normalization & Language Detection ]
        │
        ▼ (p95: 0.12 ms)
[ 2. PII Redaction (Luhn, Verhoeff, Secrets) ]
        │
        ▼ (p95: 0.90 ms)
[ 3. Rule-Based Enrichment & Tagging ]
        │
        ▼
[ Total Processing p95: 1.28 ms ]
```

| Pipeline Stage | p50 Latency | p95 Latency | Description |
|----------------|-------------|-------------|-------------|
| **Normalization** | `0.02 ms` | `0.08 ms` | HTML entity unescaping, script removal, whitespace cleanup, heuristic language match |
| **PII Redaction** | `0.03 ms` | `0.12 ms` | Regex scanning, Luhn credit card validation, Verhoeff Aadhaar checksum, API key redaction |
| **Enrichment** | `0.45 ms` | `0.90 ms` | Tokenization, negation flipping, lexicon scoring, taxonomy regex, summary generation |
| **Total Pipeline** | **0.58 ms** | **1.28 ms** | Complete in-process transformation pipeline |

---

## 5. Observations & Bottlenecks

1. **CPU Bound RegEx**: PII regex compilation and scanning is fast (< 0.15ms) because patterns are pre-compiled and heavy algorithmic checks (Luhn, Verhoeff) execute only on candidate matches.
2. **Sub-millisecond Lexicon Lookups**: The lexicon sets (`POSITIVE_LEXICON`, `NEGATIVE_LEXICON`) use O(1) hash sets with negation lookahead, allowing the pipeline to comfortably sustain >1,300 items/sec locally.
3. **Lambda Scalability Implications**: At AWS Lambda concurrency of 20 instances, processing a standard batch of 10 items will complete in < 25ms of compute time, well within the 30-second Lambda timeout window and leaving ample headroom for DynamoDB write latency.
