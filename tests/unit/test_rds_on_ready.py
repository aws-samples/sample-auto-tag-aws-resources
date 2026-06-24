"""Unit tests for the RDS/Aurora service-event tagging handler.

Exercises the pure ARN-extraction logic in tag_rds_on_ready (no AWS calls).
"""

import os
import sys

import pytest

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import tag_rds_on_ready  # noqa: E402


def _event(event_id, source_arn):
    return {
        "source": "aws.rds",
        "detail-type": "RDS DB Cluster Event",
        "detail": {
            "EventID": event_id,
            "SourceArn": source_arn,
            "SourceIdentifier": source_arn.split(":")[-1],
        },
    }


def test_arn_extracted_for_cluster_created():
    arn = "arn:aws:rds:us-east-2:123456789012:cluster:my-aurora"
    detail = _event("RDS-EVENT-0170", arn)["detail"]
    assert tag_rds_on_ready._arn_from_event(detail) == arn


def test_arn_extracted_for_instance_created():
    arn = "arn:aws:rds:us-east-2:123456789012:db:my-instance"
    detail = _event("RDS-EVENT-0005", arn)["detail"]
    assert tag_rds_on_ready._arn_from_event(detail) == arn


def test_missing_source_arn_raises():
    with pytest.raises(ValueError):
        tag_rds_on_ready._arn_from_event({"EventID": "RDS-EVENT-0005"})
