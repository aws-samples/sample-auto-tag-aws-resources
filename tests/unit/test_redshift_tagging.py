# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Redshift provisioned-cluster tagging prepare logic.

DescribeClusters does not return an ARN, so prepare builds it from the event's
account and region -- that construction is the main thing worth testing here.
"""

import json
import os
import sys

import pytest

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import redshift_tagging  # noqa: E402


def _event(response_elements=None, request_parameters=None):
    return {
        "account": "123456789012",
        "region": "us-east-2",
        "source": "aws.redshift",
        "detail": {
            "eventSource": "redshift.amazonaws.com",
            "eventName": "CreateCluster",
            "responseElements": response_elements,
            "requestParameters": request_parameters or {},
        },
    }


def _prepare(event):
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "false"
    return redshift_tagging.main(event, None)


def test_prepare_builds_the_cluster_arn_from_the_event():
    result = _prepare(_event({"clusterIdentifier": "my-dw"}))
    assert result["clusterIdentifier"] == "my-dw"
    assert result["resourceArn"] == (
        "arn:aws:redshift:us-east-2:123456789012:cluster:my-dw")


def test_prepare_falls_back_to_request_parameters():
    result = _prepare(_event({}, {"clusterIdentifier": "from-request"}))
    assert result["clusterIdentifier"] == "from-request"


def test_prepare_emits_an_uppercase_tag_list():
    result = _prepare(_event({"clusterIdentifier": "my-dw"}))
    assert {"Key": "Team", "Value": "platform"} in result["tagList"]
    assert 60 <= result["waitSeconds"] <= 120


def test_prepare_raises_when_cluster_identifier_is_absent():
    with pytest.raises(ValueError, match="clusterIdentifier"):
        _prepare(_event({}, {}))


def test_identity_recording_adds_user_and_role_tags():
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "true"
    event = _event({"clusterIdentifier": "my-dw"})
    event["detail"]["userIdentity"] = {
        "type": "AssumedRole",
        "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/sessionId",
    }
    result = redshift_tagging.main(event, None)
    keys = {t["Key"]: t["Value"] for t in result["tagList"]}
    assert keys["userId"] == "sessionId"
    assert keys["roleId"] == "MyRole"


def test_check_reports_ready_only_when_available(monkeypatch):
    class _FakeRedshift:
        def describe_clusters(self, ClusterIdentifier):
            return {"Clusters": [{"ClusterStatus": "creating"}]}

    monkeypatch.setattr(redshift_tagging.boto3, "client",
                        lambda _name: _FakeRedshift())
    result = redshift_tagging.main(
        {"action": "check", "clusterIdentifier": "my-dw"}, None)
    assert result == {"ready": False, "status": "creating"}
