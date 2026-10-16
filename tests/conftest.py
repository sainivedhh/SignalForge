import os

import pytest


@pytest.fixture(autouse=True)
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["FEEDBACK_TABLE_NAME"] = "TestFeedbackTable"
    os.environ["ALERTS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123456789012:test-feedback-alerts"
