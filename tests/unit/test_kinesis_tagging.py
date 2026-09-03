# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Kinesis tagging prepare logic.

Kinesis CreateStream returns an empty body, so the stream name has to come out
of requestParameters. AddTagsToStream also takes a {key: value} map rather than
a TagList, so prepare emits tagMap, not tagList. No AWS calls here.
"""

import json
import os
import sys

import pytest

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import kinesis_tagging  # noqa: E402


def _event(request_parameters, response_elements=None, event_name="CreateStream"):
    return {
        "account": "123456789012",
        "region": "us-east-2",
        "source": "aws.kinesis",
        "detail": {
            "eventSource": "kinesis.amazonaws.com",
            "eventName": event_name,
            "responseElements": response_elements,
            "requestParameters": request_parameters,
        },
    }


def _prepare(event):
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "false"
    return kinesis_tagging.main(event, None)


def test_prepare_reads_stream_name_from_request_parameters():
    """CreateStream has responseElements == None."""
    result = _prepare(_event({"streamName": "my-stream"}, None))
    assert result["streamName"] == "my-stream"


def test_prepare_emits_a_tag_map_not_a_tag_list():
    result = _prepare(_event({"streamName": "my-stream"}))
    assert result["tagMap"] == {"Team": "platform"}
    assert "tagList" not in result


def test_prepare_randomizes_the_first_wait():
    result = _prepare(_event({"streamName": "my-stream"}))
    assert 15 <= result["waitSeconds"] <= 45


def test_prepare_raises_when_stream_name_is_absent():
    with pytest.raises(ValueError, match="streamName"):
        _prepare(_event({}, None))


def test_identity_recording_adds_user_and_role_tags():
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "true"
    event = _event({"streamName": "my-stream"})
    event["detail"]["userIdentity"] = {
        "type": "AssumedRole",
        "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/sessionId",
    }
    result = kinesis_tagging.main(event, None)
    assert result["tagMap"]["userId"] == "sessionId"
    assert result["tagMap"]["roleId"] == "MyRole"


def test_check_reports_ready_only_when_active(monkeypatch):
    class _FakeKinesis:
        def describe_stream_summary(self, StreamName):
            return {"StreamDescriptionSummary": {
                "StreamStatus": "CREATING",
                "StreamARN": "arn:aws:kinesis:us-east-2:123456789012:stream/my-stream",
            }}

    monkeypatch.setattr(kinesis_tagging.boto3, "client",
                        lambda _name: _FakeKinesis())
    result = kinesis_tagging.main(
        {"action": "check", "streamName": "my-stream"}, None)
    assert result == {
        "ready": False,
        "status": "CREATING",
        "arn": "arn:aws:kinesis:us-east-2:123456789012:stream/my-stream",
    }
