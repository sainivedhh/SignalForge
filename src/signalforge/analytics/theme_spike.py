"""Theme discovery and spike detection module.

Features:
- Clusters feedback using TF-IDF + Agglomerative Clustering (explainable, offline, no network calls).
- Tracks negative feedback counts over sliding time windows per theme.
- Detects volume spikes against a rolling baseline (e.g. > 2.0x baseline or sudden surge).
- Dispatches SNS alerts with cooldown periods to prevent alert fatigue.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer

logger = logging.getLogger(__name__)

_sns_client = None


def get_sns_client() -> Any:
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _sns_client


class ThemeClusterer:
    """Clustering engine using TF-IDF vectorization and Agglomerative Clustering.

    Offline, deterministic, explainable theme extraction based on top keywords.
    """
    def __init__(self, n_clusters: int = 3, distance_threshold: Optional[float] = None) -> None:
        self.n_clusters = n_clusters
        self.distance_threshold = distance_threshold
        self.vectorizer = TfidfVectorizer(
            stop_words="english",
            max_features=500,
            ngram_range=(1, 1),
            token_pattern=r"(?u)\b[a-zA-Z]{3,}\b",
            sublinear_tf=True,
        )

    def fit_predict(self, texts: List[str]) -> Tuple[List[str], Dict[str, str]]:
        """Fit clusters on feedback texts.

        Returns:
            cluster_labels: List of cluster IDs per item (e.g. 'theme_0', 'theme_1').
            theme_names: Mapping from cluster ID to human-readable descriptive name (top TF-IDF terms).
        """
        if not texts:
            return [], {}

        if len(texts) < self.n_clusters:
            # Fallback if insufficient items: 1 theme per item or single theme
            theme_names = {"theme_0": "General Feedback"}
            return ["theme_0" for _ in texts], theme_names

        X = self.vectorizer.fit_transform(texts)
        if self.distance_threshold is not None:
            clustering = AgglomerativeClustering(
                n_clusters=None,
                distance_threshold=self.distance_threshold,
                metric="cosine",
                linkage="average",
            )
        else:
            n_c = min(self.n_clusters, len(texts))
            clustering = AgglomerativeClustering(
                n_clusters=n_c,
                metric="cosine",
                linkage="average",
            )

        labels = clustering.fit_predict(X.toarray())
        cluster_labels = [f"theme_{lbl}" for lbl in labels]

        # Extract top 3 keywords per cluster for explainability
        feature_names = np.array(self.vectorizer.get_feature_names_out())
        theme_names = {}
        unique_labels = set(labels)

        for ul in unique_labels:
            indices = np.where(labels == ul)[0]
            cluster_center = np.mean(X[indices].toarray(), axis=0)
            top_kw_idx = np.argsort(cluster_center)[::-1][:3]
            top_words = feature_names[top_kw_idx]
            theme_title = " / ".join([w.capitalize() for w in top_words if cluster_center[w == feature_names][0] > 0])
            theme_names[f"theme_{ul}"] = theme_title or f"Theme {ul}"

        return cluster_labels, theme_names


class SpikeDetector:
    """Detects volume surges in negative sentiment per theme with cooldown protection."""
    def __init__(
        self,
        baseline_multiplier: float = 2.0,
        min_negative_threshold: int = 5,
        cooldown_seconds: float = 300.0,
    ) -> None:
        self.baseline_multiplier = baseline_multiplier
        self.min_negative_threshold = min_negative_threshold
        self.cooldown_seconds = cooldown_seconds
        # In-memory alert cooldown tracker: {theme_id: last_alert_epoch_seconds}
        self._last_alert_time: Dict[str, float] = {}

    def evaluate_spike(
        self,
        theme_id: str,
        current_negative_count: int,
        rolling_baseline_avg: float,
        current_epoch: float,
    ) -> bool:
        """Return True if current count exceeds spike threshold and cooldown is satisfied."""
        if current_negative_count < self.min_negative_threshold:
            return False

        # Threshold is maximum of min_negative_threshold or baseline * multiplier
        threshold = max(float(self.min_negative_threshold), rolling_baseline_avg * self.baseline_multiplier)
        if current_negative_count < threshold:
            return False

        # Check cooldown
        last_alert = self._last_alert_time.get(theme_id, 0.0)
        if current_epoch - last_alert < self.cooldown_seconds:
            logger.info("Spike detected on %s but suppressed by cooldown", theme_id)
            return False

        return True

    def publish_alert(
        self,
        topic_arn: str,
        theme_id: str,
        theme_name: str,
        current_negative_count: int,
        baseline_avg: float,
        current_epoch: float,
    ) -> Optional[str]:
        """Publish SNS alert for detected spike."""
        if not self.evaluate_spike(theme_id, current_negative_count, baseline_avg, current_epoch):
            return None

        sns = get_sns_client()
        message = {
            "alert": "ANOMALOUS_NEGATIVE_FEEDBACK_SPIKE",
            "theme_id": theme_id,
            "theme_name": theme_name,
            "negative_count": current_negative_count,
            "baseline_avg": round(baseline_avg, 2),
            "surge_ratio": round(current_negative_count / max(baseline_avg, 1.0), 2),
            "timestamp": datetime.fromtimestamp(current_epoch, tz=timezone.utc).isoformat(),
        }

        try:
            res = sns.publish(
                TopicArn=topic_arn,
                Subject=f"ALERT: Feedback Spike in '{theme_name}'",
                Message=json.dumps(message),
            )
            self._last_alert_time[theme_id] = current_epoch
            logger.info("Published SNS spike alert for %s: %s", theme_id, res.get("MessageId"))
            return res.get("MessageId")
        except Exception as e:
            logger.exception("Failed to publish SNS spike alert: %s", e)
            return None
