"""Unit tests for the ElastiCache tagging prepare logic.

ElastiCache is now tagged asynchronously by a Step Functions workflow that
polls Describe* until the resource is available. This exercises the pure
event-parsing logic in elasticache_tagging._prepare / _extract_resource for the
three supported Create* events (no AWS calls).
"""

import os
import sys
import json
import importlib

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "lambda")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import elasticache_tagging  # noqa: E402


def _event(event_name, response_elements, request_parameters=None):
    return {
        "account": "123456789012",
        "region": "us-east-2",
        "source": "aws.elasticache",
        "detail": {
            "eventSource": "elasticache.amazonaws.com",
            "eventName": event_name,
            "responseElements": response_elements,
            "requestParameters": request_parameters or {},
        },
    }


def _prepare(event):
    """Invoke the prepare path with a known tags env var."""
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "false"
    return elasticache_tagging.main(event, None)


def test_prepare_serverless_cache():
    event = _event("CreateServerlessCache", {
        "serverlessCache": {"serverlessCacheName": "my-cache", "status": "CREATING"}
    })
    result = _prepare(event)
    assert result["resourceType"] == "serverlessCache"
    assert result["resourceId"] == "my-cache"
    assert {"Key": "Team", "Value": "platform"} in result["tagList"]
    assert 60 <= result["waitSeconds"] <= 120


def test_prepare_cache_cluster():
    event = _event("CreateCacheCluster", {"cacheClusterId": "cc-1"})
    result = _prepare(event)
    assert result["resourceType"] == "cacheCluster"
    assert result["resourceId"] == "cc-1"


def test_prepare_replication_group():
    event = _event("CreateReplicationGroup", {"replicationGroupId": "rg-1"})
    result = _prepare(event)
    assert result["resourceType"] == "replicationGroup"
    assert result["resourceId"] == "rg-1"


def test_prepare_falls_back_to_request_parameters():
    """If responseElements lacks the id, fall back to requestParameters."""
    event = _event("CreateCacheCluster", {}, {"cacheClusterId": "cc-from-request"})
    result = _prepare(event)
    assert result["resourceId"] == "cc-from-request"


def test_identity_recording_adds_user_tag():
    os.environ["tags"] = json.dumps({"Team": "platform"})
    os.environ["identityRecording"] = "true"
    event = _event("CreateCacheCluster", {"cacheClusterId": "cc-1"})
    event["detail"]["userIdentity"] = {
        "type": "AssumedRole",
        "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/sessionId",
    }
    result = elasticache_tagging.main(event, None)
    keys = {t["Key"]: t["Value"] for t in result["tagList"]}
    assert keys.get("userId") == "sessionId"
    assert keys.get("roleId") == "MyRole"
