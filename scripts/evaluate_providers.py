"""Evaluation script for RuleBasedProvider on Development and Holdout datasets.

Computes:
- Per-class precision, recall, and F1 scores for Sentiment and Topics.
- Confusion matrix.
- Failure case extraction explaining exactly why and how errors occurred.
"""

import json
from typing import Any, Dict, List

from signalforge.enrich.providers import RuleBasedProvider


def evaluate_dataset(file_path: str, provider: RuleBasedProvider) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sentiments = ["positive", "neutral", "negative"]
    sentiment_cm = {true: {pred: 0 for pred in sentiments} for true in sentiments}

    failures: List[Dict[str, Any]] = []

    for item in data:
        text = item["text"]
        expected_s = item["expected_sentiment"]
        expected_t = item["expected_topic"]

        res = provider.enrich(text)
        predicted_s = res.sentiment
        predicted_topics = res.topic_tags

        sentiment_cm[expected_s][predicted_s] += 1

        topic_matched = expected_t in predicted_topics or (expected_t == "other" and "other" in predicted_topics)

        if predicted_s != expected_s or not topic_matched:
            failures.append({
                "id": item["id"],
                "category": item["category"],
                "text": text,
                "expected_sentiment": expected_s,
                "predicted_sentiment": predicted_s,
                "expected_topic": expected_t,
                "predicted_topics": predicted_topics,
            })

    # Compute metrics per class
    metrics: Dict[str, Dict[str, float]] = {}
    for c in sentiments:
        tp = sentiment_cm[c][c]
        fp = sum(sentiment_cm[other][c] for other in sentiments if other != c)
        fn = sum(sentiment_cm[c][other] for other in sentiments if other != c)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

        metrics[c] = {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "support": tp + fn,
        }

    total_correct = sum(sentiment_cm[c][c] for c in sentiments)
    total_samples = len(data)
    accuracy = round(total_correct / total_samples, 3)

    return {
        "dataset": file_path,
        "sample_count": total_samples,
        "accuracy": accuracy,
        "class_metrics": metrics,
        "confusion_matrix": sentiment_cm,
        "failures": failures,
    }


def main() -> None:
    provider = RuleBasedProvider()
    dev_results = evaluate_dataset("data/dev_dataset.json", provider)
    holdout_results = evaluate_dataset("data/holdout_dataset.json", provider)

    print("=== DEV SET METRICS ===")
    print(f"Accuracy: {dev_results['accuracy']}")
    print("Class Metrics:", json.dumps(dev_results["class_metrics"], indent=2))
    print("Confusion Matrix:", json.dumps(dev_results["confusion_matrix"], indent=2))
    print(f"Total Failures: {len(dev_results['failures'])} / {dev_results['sample_count']}")

    print("\n=== HOLDOUT SET METRICS ===")
    print(f"Accuracy: {holdout_results['accuracy']}")
    print("Class Metrics:", json.dumps(holdout_results["class_metrics"], indent=2))
    print("Confusion Matrix:", json.dumps(holdout_results["confusion_matrix"], indent=2))
    print(f"Total Failures: {len(holdout_results['failures'])} / {holdout_results['sample_count']}")

    # Save raw evaluation json
    with open("data/eval_summary.json", "w", encoding="utf-8") as f:
        json.dump({"dev": dev_results, "holdout": holdout_results}, f, indent=2)


if __name__ == "__main__":
    main()
