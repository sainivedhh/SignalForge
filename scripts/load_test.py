"""Local simulated concurrency load testing harness for SignalForge.

Generates N synthetic feedback items and processes them locally in-process
with simulated concurrency (ThreadPoolExecutor), measuring throughput (items/sec)
and p50 / p95 / p99 latencies per pipeline stage (normalization, redaction, enrichment, overall).
"""

import argparse
import platform
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
from signalforge.enrich.normalizer import normalize_text
from signalforge.enrich.pii_redactor import redact_pii
from signalforge.enrich.providers import RuleBasedProvider
from signalforge.models import IngestMessage

SAMPLE_FEEDBACKS = [
    "Customer service was fantastic! Solved my ticket in under 5 minutes.",
    "Terrible experience. App crashed during checkout and my card 4532-0151-1283-0366 was charged twice. Reach me at victim@example.com.",
    "Where do I find the CSV export button in the new dashboard update? Please help.",
    "Horrible latency, spinning wheel on every page load. Fix this sluggish performance ASAP!",
    "Can you please add dark mode support and 2FA authentication via Google Authenticator?",
    "Payment gateway failed with 500 error code. Very frustrating.",
    "Smooth and intuitive design, my team loves the collaboration feature.",
    "Aadhaar verification failed with ID 2121 5050 8894. Please look into this urgent bug.",
]


def process_item_stages(msg: IngestMessage, provider: RuleBasedProvider) -> Dict[str, float]:
    """Execute pipeline stages and record timing in milliseconds."""
    t0 = time.perf_counter()
    clean_text, _, _ = normalize_text(msg.text)
    t1 = time.perf_counter()

    redacted_text, _ = redact_pii(clean_text)
    t2 = time.perf_counter()

    _ = provider.enrich(redacted_text)
    t3 = time.perf_counter()

    return {
        "norm_ms": (t1 - t0) * 1000,
        "pii_ms": (t2 - t1) * 1000,
        "enrich_ms": (t3 - t2) * 1000,
        "total_ms": (t3 - t0) * 1000,
    }


def run_load_test(num_items: int = 500, concurrency: int = 8) -> Dict[str, Any]:
    provider = RuleBasedProvider()
    messages: List[IngestMessage] = []

    for i in range(num_items):
        raw_text = SAMPLE_FEEDBACKS[i % len(SAMPLE_FEEDBACKS)]
        msg = IngestMessage(
            feedback_id=f"fb_load_{i:06d}",
            source=["zendesk", "twitter", "email", "playstore"][i % 4],
            text=f"{raw_text} (item {i})",
            customer_id=f"cust_{i % 50}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        messages.append(msg)

    print(f"Starting load test: {num_items} items, {concurrency} worker threads...")
    start_time = time.perf_counter()

    stage_timings: List[Dict[str, float]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(process_item_stages, msg, provider) for msg in messages]
        for f in as_completed(futures):
            stage_timings.append(f.result())

    wall_duration = time.perf_counter() - start_time
    throughput = num_items / wall_duration

    totals = [m["total_ms"] for m in stage_timings]
    norms = [m["norm_ms"] for m in stage_timings]
    piis = [m["pii_ms"] for m in stage_timings]
    enriches = [m["enrich_ms"] for m in stage_timings]

    metrics = {
        "num_items": num_items,
        "concurrency": concurrency,
        "wall_duration_s": round(wall_duration, 3),
        "throughput_items_per_sec": round(throughput, 1),
        "total_p50_ms": round(float(np.percentile(totals, 50)), 2),
        "total_p95_ms": round(float(np.percentile(totals, 95)), 2),
        "total_p99_ms": round(float(np.percentile(totals, 99)), 2),
        "norm_p50_ms": round(float(np.percentile(norms, 50)), 2),
        "norm_p95_ms": round(float(np.percentile(norms, 95)), 2),
        "pii_p50_ms": round(float(np.percentile(piis, 50)), 2),
        "pii_p95_ms": round(float(np.percentile(piis, 95)), 2),
        "enrich_p50_ms": round(float(np.percentile(enriches, 50)), 2),
        "enrich_p95_ms": round(float(np.percentile(enriches, 95)), 2),
        "system": {
            "os": f"{platform.system()} {platform.release()}",
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
    }

    print("\n=== LOAD TEST RESULTS ===")
    print(f"Throughput: {metrics['throughput_items_per_sec']} items/second")
    print(f"Total Latency: p50={metrics['total_p50_ms']}ms, p95={metrics['total_p95_ms']}ms, p99={metrics['total_p99_ms']}ms")
    print(f"Stage Breakdown (p95): Normalization={metrics['norm_p95_ms']}ms, PII Redaction={metrics['pii_p95_ms']}ms, Enrichment={metrics['enrich_p95_ms']}ms")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run local simulated concurrency load test")
    parser.add_argument("--items", type=int, default=500, help="Total items to process")
    parser.add_argument("--concurrency", type=int, default=8, help="Number of worker threads")
    args = parser.parse_args()
    run_load_test(args.items, args.concurrency)
