"""DLQ Redrive script for dead-lettered SQS feedback messages.

Retrieves messages from DLQ, allows optional filtering or verification,
and re-queues them back into the primary processing queue.
"""

import argparse
import logging
from typing import Any, Dict, List

import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def redrive_dlq(dlq_url: str, main_queue_url: str, max_messages: int = 100) -> int:
    """Move up to max_messages from dlq_url to main_queue_url."""
    sqs = boto3.client("sqs")
    redriven_count = 0

    while redriven_count < max_messages:
        batch_size = min(10, max_messages - redriven_count)
        response = sqs.receive_message(
            QueueUrl=dlq_url,
            MaxNumberOfMessages=batch_size,
            WaitTimeSeconds=1,
            VisibilityTimeout=30,
        )
        messages = response.get("Messages", [])
        if not messages:
            break

        entries: List[Dict[str, Any]] = []
        for i, msg in enumerate(messages):
            entries.append({
                "Id": f"redrive_{i}_{msg['MessageId'][:8]}",
                "MessageBody": msg["Body"],
            })

        send_res = sqs.send_message_batch(QueueUrl=main_queue_url, Entries=entries)
        if send_res.get("Failed"):
            logger.error("Failed to re-queue some messages: %s", send_res["Failed"])

        # Delete successfully redriven messages from DLQ
        successful_ids = {e["Id"] for e in send_res.get("Successful", [])}
        delete_entries = [
            {"Id": f"del_{i}", "ReceiptHandle": messages[i]["ReceiptHandle"]}
            for i, e in enumerate(entries)
            if e["Id"] in successful_ids
        ]
        if delete_entries:
            sqs.delete_message_batch(QueueUrl=dlq_url, Entries=delete_entries)
            redriven_count += len(delete_entries)

    logger.info("Successfully redriven %d messages from DLQ", redriven_count)
    return redriven_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Redrive messages from DLQ to Main SQS Queue")
    parser.add_argument("--dlq-url", required=True, help="DLQ URL")
    parser.add_argument("--main-url", required=True, help="Main Queue URL")
    parser.add_argument("--limit", type=int, default=100, help="Max messages to redrive")
    args = parser.parse_args()
    redrive_dlq(args.dlq_url, args.main_url, args.limit)
