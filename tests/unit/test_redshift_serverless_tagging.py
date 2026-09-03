# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Redshift Serverless *workgroup* tagging prepare logic.

The namespace half of Redshift Serverless is tagged immediately by the generic
Lambda (it is AVAILABLE on creation); only the workgroup needs this polling
workflow. No AWS calls here.
"""

import json
import os
import sys

import pytest

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import redshift_serverless_tagging as rss  # noqa: E402


def _event(response_elements=None, request_parameters=None):
    return {
        "account": "123456789012",
        "region": "us-east-2",
        "source": "aws.redshift-serverless",
        "detail": {
            "eventSource": "redshift-serverless.amazonaws.com",
            "eventName": "CreateWorkgroup",
            "responseElements": response_elements,
            "requestParameters": request_parameters or {},
        },
    }


def _prepare(event):
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "false"
    return rss.main(event, None)


def test_prepare_reads_the_workgroup_name():
    result = _prepare(_event(
        {"workgroup": {"workgroupName": "wg-1", "status": "CREATING"}}))
    assert result["workgroupName"] == "wg-1"
    assert {"Key": "Team", "Value": "platform"} in result["tagList"]
    assert 60 <= result["waitSeconds"] <= 120


def test_prepare_falls_back_to_request_parameters():
    result = _prepare(_event({}, {"workgroupName": "from-request"}))
    assert result["workgroupName"] == "from-request"


def test_prepare_raises_when_workgroup_name_is_absent():
    with pytest.raises(ValueError, match="workgroupName"):
        _prepare(_event({}, {}))


def test_identity_recording_adds_user_and_role_tags():
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "true"
    event = _event({"workgroup": {"workgroupName": "wg-1"}})
    event["detail"]["userIdentity"] = {
        "type": "AssumedRole",
        "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/sessionId",
    }
    result = rss.main(event, None)
    keys = {t["Key"]: t["Value"] for t in result["tagList"]}
    assert keys["userId"] == "sessionId"
    assert keys["roleId"] == "MyRole"


def test_check_reports_ready_only_when_available(monkeypatch):
    arn = "arn:aws:redshift-serverless:us-east-2:123456789012:workgroup/wg-1"

    class _FakeRss:
        def get_workgroup(self, workgroupName):
            return {"workgroup": {"status": "CREATING", "workgroupArn": arn}}

    monkeypatch.setattr(rss.boto3, "client", lambda _name: _FakeRss())
    result = rss.main({"action": "check", "workgroupName": "wg-1"}, None)
    assert result == {"ready": False, "status": "CREATING", "arn": arn}
