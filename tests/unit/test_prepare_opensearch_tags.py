"""Unit tests for the OpenSearch prepare-tags Lambda.

These tests exercise pure logic (no AWS calls), so they can run without
aws-cdk-lib installed.
"""

import os
import sys
import json
import importlib

# Make the lambda/ directory importable.
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

prepare = importlib.import_module("prepare_opensearch_tags")


def _base_event(event_name, domain_name="my-domain", identity_type="IAMUser",
                arn="arn:aws:iam::123456789012:user/alice"):
    return {
        "source": "aws.es",
        "detail": {
            "eventSource": "es.amazonaws.com",
            "eventName": event_name,
            "userIdentity": {"type": identity_type, "arn": arn},
            "responseElements": {
                "domainStatus": {"domainName": domain_name}
            },
        },
    }


def test_extract_new_api_create_domain():
    os.environ["tags"] = json.dumps({"team": "data"})
    os.environ["identityRecording"] = "false"
    result = prepare.main(_base_event("CreateDomain"), None)
    assert result["domainName"] == "my-domain"
    assert {"Key": "team", "Value": "data"} in result["tagList"]


def test_extract_legacy_api_create_elasticsearch_domain():
    os.environ["tags"] = json.dumps({"team": "data"})
    os.environ["identityRecording"] = "false"
    result = prepare.main(_base_event("CreateElasticsearchDomain"), None)
    assert result["domainName"] == "my-domain"


def test_fallback_to_request_parameters():
    os.environ["tags"] = json.dumps({"env": "prod"})
    os.environ["identityRecording"] = "false"
    event = {
        "detail": {
            "eventName": "CreateDomain",
            "requestParameters": {"domainName": "from-request"},
            "responseElements": None,
        }
    }
    result = prepare.main(event, None)
    assert result["domainName"] == "from-request"


def test_identity_recording_assumed_role():
    os.environ["tags"] = json.dumps({"env": "prod"})
    os.environ["identityRecording"] = "true"
    event = _base_event(
        "CreateDomain",
        identity_type="AssumedRole",
        arn="arn:aws:sts::123456789012:assumed-role/MyRole/session-name",
    )
    result = prepare.main(event, None)
    keys = {t["Key"]: t["Value"] for t in result["tagList"]}
    assert keys["userId"] == "session-name"
    assert keys["roleId"] == "MyRole"


def test_tag_list_format():
    os.environ["tags"] = json.dumps({"a": "1", "b": "2"})
    os.environ["identityRecording"] = "false"
    result = prepare.main(_base_event("CreateDomain"), None)
    for tag in result["tagList"]:
        assert set(tag.keys()) == {"Key", "Value"}
        assert isinstance(tag["Key"], str)
        assert isinstance(tag["Value"], str)
