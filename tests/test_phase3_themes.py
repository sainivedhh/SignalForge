"""Tests for Phase 3: Theme Clustering and Trend Spike Detection with SNS."""

import boto3
from moto import mock_aws
from signalforge.analytics.theme_spike import SpikeDetector, ThemeClusterer


def test_theme_clustering_explainability():
    texts = [
        "The mobile app crashes every time I open the camera",
        "App crash on camera launch on Android mobile",
        "Crashing when attempting to take picture in mobile view",
        "Billing charged me twice for monthly subscription fee",
        "Payment charged extra subscription fee billing issue",
        "Overcharged on my invoice billing charge payment",
        "Login button does not respond on the sign in page",
        "Cannot sign in, authentication failed password login",
    ]

    clusterer = ThemeClusterer(n_clusters=3)
    labels, theme_names = clusterer.fit_predict(texts)

    assert len(labels) == len(texts)
    assert len(theme_names) == 3
    # Same topics cluster together
    assert labels[0] == labels[1] == labels[2]  # Crash / camera
    assert labels[3] == labels[4] == labels[5]  # Billing / charge
    assert labels[6] == labels[7]                # Login / auth
    # Themes have human-readable explainable titles
    for _t_id, t_name in theme_names.items():
        assert len(t_name) > 0


@mock_aws
def test_spike_detection_and_sns_cooldown():
    # Setup Moto SNS
    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="feedback-spike-alerts")
    topic_arn = topic["TopicArn"]

    detector = SpikeDetector(
        baseline_multiplier=2.0,
        min_negative_threshold=5,
        cooldown_seconds=300.0,
    )

    t0 = 1000.0
    # 1. Normal count below threshold
    res1 = detector.publish_alert(
        topic_arn=topic_arn,
        theme_id="theme_billing",
        theme_name="Billing Charges",
        current_negative_count=3,
        baseline_avg=2.0,
        current_epoch=t0,
    )
    assert res1 is None

    # 2. Spike! count=10 vs baseline=2.0 (5x surge > 2.0x and >= 5)
    res2 = detector.publish_alert(
        topic_arn=topic_arn,
        theme_id="theme_billing",
        theme_name="Billing Charges",
        current_negative_count=10,
        baseline_avg=2.0,
        current_epoch=t0 + 60.0,
    )
    assert res2 is not None  # Alert successfully published to SNS

    # 3. Repeated spike during cooldown period (t0 + 120s, within 300s window)
    res3 = detector.publish_alert(
        topic_arn=topic_arn,
        theme_id="theme_billing",
        theme_name="Billing Charges",
        current_negative_count=12,
        baseline_avg=2.0,
        current_epoch=t0 + 120.0,
    )
    assert res3 is None  # Suppressed by cooldown

    # 4. After cooldown expires (t0 + 400s)
    res4 = detector.publish_alert(
        topic_arn=topic_arn,
        theme_id="theme_billing",
        theme_name="Billing Charges",
        current_negative_count=14,
        baseline_avg=2.0,
        current_epoch=t0 + 400.0,
    )
    assert res4 is not None  # Successfully fired again
