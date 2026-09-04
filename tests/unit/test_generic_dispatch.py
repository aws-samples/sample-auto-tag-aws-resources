# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dispatch tests for the generic immediate-tagging Lambda.

`lambda-handler.py` has a dash in its filename so it cannot be imported by
module name; it is loaded from its path. All AWS calls are faked -- these tests
assert *which* client and operation the handler chooses and with exactly what
payload, which is the part that differs per service.
"""

import importlib.util
import json
import os

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lambda"))


def _load_handler_module():
    spec = importlib.util.spec_from_file_location(
        "generic_lambda_handler", os.path.join(LAMBDA_DIR, "lambda-handler.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lambda_handler = _load_handler_module()


class _FakeClient:
    """Records every operation invoked on it as (client_name, op, kwargs)."""

    def __init__(self, name, calls):
        self._name = name
        self._calls = calls

    def __getattr__(self, op):
        def _call(**kwargs):
            self._calls.append((self._name, op, kwargs))
            return {}
        return _call


class _FakeBoto3:
    def __init__(self):
        self.calls = []
        self.clients = []

    def client(self, name, *args, **kwargs):
        self.clients.append(name)
        return _FakeClient(name, self.calls)


@pytest.fixture
def fake_boto3(monkeypatch):
    fake = _FakeBoto3()
    monkeypatch.setattr(lambda_handler, "boto3", fake)
    monkeypatch.setenv("tags", json.dumps({"Team": "platform"}))
    monkeypatch.setenv("identityRecording", "false")
    return fake


def _event(source, event_source, event_name, response_elements=None,
           request_parameters=None):
    return {
        "account": "123456789012",
        "region": "us-east-2",
        "source": source,
        "detail": {
            "eventSource": event_source,
            "eventName": event_name,
            "responseElements": response_elements,
            "requestParameters": request_parameters or {},
        },
    }


def test_gamelift_is_tagged_natively_and_not_also_via_rgt(fake_boto3):
    """Regression: the old if/if/else chain let GameLift fall through to the
    Resource Groups Tagging API after it had already been tagged natively."""
    event = _event("aws.gamelift", "gamelift.amazonaws.com", "CreateFleet",
                   {"fleetAttributes": {"fleetArn": "arn:aws:gamelift:us-east-2:123456789012:fleet/f-1"}})
    lambda_handler.main(event, None)

    assert "resourcegroupstaggingapi" not in fake_boto3.clients
    assert fake_boto3.calls == [(
        "gamelift", "tag_resource",
        {"ResourceARN": "arn:aws:gamelift:us-east-2:123456789012:fleet/f-1",
         "Tags": [{"Key": "Team", "Value": "platform"}]},
    )]


def test_logs_is_tagged_by_log_group_name(fake_boto3):
    event = _event("aws.logs", "logs.amazonaws.com", "CreateLogGroup",
                   None, {"logGroupName": "/my/group"})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "logs", "tag_log_group",
        {"logGroupName": "/my/group", "tags": {"Team": "platform"}},
    )]


def test_source_without_native_tagger_uses_rgt(fake_boto3):
    event = _event("aws.s3", "s3.amazonaws.com", "CreateBucket",
                   None, {"bucketName": "my-bucket"})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "resourcegroupstaggingapi", "tag_resources",
        {"ResourceARNList": ["arn:aws:s3:::my-bucket"],
         "Tags": {"Team": "platform"}},
    )]


def test_no_resolved_arns_does_not_call_rgt(fake_boto3):
    """A handler returns None when the eventName does not match; the old code
    passed that straight to tag_resources, which rejects an empty ARN list."""
    event = _event("aws.s3", "s3.amazonaws.com", "DeleteBucket",
                   None, {"bucketName": "my-bucket"})
    result = lambda_handler.main(event, None)

    assert fake_boto3.calls == []
    assert result["statusCode"] == 200


def test_unsupported_source_is_rejected(fake_boto3):
    event = _event("aws.notaservice", "notaservice.amazonaws.com", "CreateThing")
    with pytest.raises(ValueError, match="Unsupported event source"):
        lambda_handler.main(event, None)


def test_msk_cluster_tagged_with_tag_map(fake_boto3):
    arn = "arn:aws:kafka:us-east-2:123456789012:cluster/my-cluster/uuid-1"
    event = _event("aws.kafka", "kafka.amazonaws.com", "CreateClusterV2",
                   {"clusterArn": arn})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "kafka", "tag_resource",
        {"ResourceArn": arn, "Tags": {"Team": "platform"}},
    )]


def test_msk_also_handles_v1_create_cluster(fake_boto3):
    arn = "arn:aws:kafka:us-east-2:123456789012:cluster/my-cluster/uuid-1"
    event = _event("aws.kafka", "kafka.amazonaws.com", "CreateCluster",
                   {"clusterArn": arn})
    lambda_handler.main(event, None)
    assert fake_boto3.calls[0][0] == "kafka"


def test_eks_cluster_tagged_with_lowercase_map_params(fake_boto3):
    arn = "arn:aws:eks:us-east-2:123456789012:cluster/my-eks"
    event = _event("aws.eks", "eks.amazonaws.com", "CreateCluster",
                   {"cluster": {"arn": arn, "status": "CREATING"}})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "eks", "tag_resource",
        {"resourceArn": arn, "tags": {"Team": "platform"}},
    )]


def test_missing_arn_in_response_tags_nothing(fake_boto3):
    """A malformed/partial responseElements must not raise -- just no-op."""
    event = _event("aws.eks", "eks.amazonaws.com", "CreateCluster", {})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_ecs_cluster_tagged_with_lowercase_tag_list(fake_boto3):
    arn = "arn:aws:ecs:us-east-2:123456789012:cluster/my-cluster"
    event = _event("aws.ecs", "ecs.amazonaws.com", "CreateCluster",
                   {"cluster": {"clusterArn": arn}})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "ecs", "tag_resource",
        {"resourceArn": arn, "tags": [{"key": "Team", "value": "platform"}]},
    )]


def test_ecs_service_uses_the_long_service_arn(fake_boto3):
    arn = "arn:aws:ecs:us-east-2:123456789012:service/my-cluster/my-service"
    event = _event("aws.ecs", "ecs.amazonaws.com", "CreateService",
                   {"service": {"serviceArn": arn}})
    lambda_handler.main(event, None)

    assert fake_boto3.calls[0][2]["resourceArn"] == arn


def test_tag_with_retry_retries_listed_error_codes(monkeypatch):
    """A just-created ECS service can be invisible to the tagging API."""
    monkeypatch.setattr(lambda_handler.time, "sleep", lambda _s: None)
    attempts = []

    def _flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise lambda_handler.ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}}, "TagResource")
        return "ok"

    result = lambda_handler._tag_with_retry(
        _flaky, ("ResourceNotFoundException",), attempts=5, delay=0)
    assert result == "ok"
    assert len(attempts) == 3


def test_tag_with_retry_reraises_other_error_codes(monkeypatch):
    monkeypatch.setattr(lambda_handler.time, "sleep", lambda _s: None)

    def _bad_request():
        raise lambda_handler.ClientError(
            {"Error": {"Code": "InvalidParameterException"}}, "TagResource")

    with pytest.raises(lambda_handler.ClientError):
        lambda_handler._tag_with_retry(
            _bad_request, ("ResourceNotFoundException",), attempts=5, delay=0)


def test_msf_application_tagged_with_uppercase_tag_list(fake_boto3):
    arn = "arn:aws:kinesisanalytics:us-east-2:123456789012:application/my-app"
    event = _event("aws.kinesisanalytics", "kinesisanalytics.amazonaws.com",
                   "CreateApplication",
                   {"applicationDetail": {"applicationARN": arn}})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "kinesisanalyticsv2", "tag_resource",
        {"ResourceARN": arn, "Tags": [{"Key": "Team", "Value": "platform"}]},
    )]


def test_msf_falls_back_to_top_level_application_arn(fake_boto3):
    arn = "arn:aws:kinesisanalytics:us-east-2:123456789012:application/my-app"
    event = _event("aws.kinesisanalytics", "kinesisanalytics.amazonaws.com",
                   "CreateApplication", {"applicationARN": arn})
    lambda_handler.main(event, None)
    assert fake_boto3.calls[0][2]["ResourceARN"] == arn


def test_redshift_serverless_namespace_tagged_with_lowercase_tag_list(fake_boto3):
    arn = "arn:aws:redshift-serverless:us-east-2:123456789012:namespace/ns-1"
    event = _event("aws.redshift-serverless", "redshift-serverless.amazonaws.com",
                   "CreateNamespace",
                   {"namespace": {"namespaceArn": arn, "status": "AVAILABLE"}})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "redshift-serverless", "tag_resource",
        {"resourceArn": arn, "tags": [{"key": "Team", "value": "platform"}]},
    )]


def test_glue_database_arn_is_built_from_the_database_input(fake_boto3):
    """No Glue Create* returns an ARN, so it is rebuilt from account/region.
    Glue's tag parameter is TagsToAdd -- a map, not a Tags list."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateDatabase",
                   {}, {"databaseInput": {"name": "analytics_db"}})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "glue", "tag_resource",
        {"ResourceArn": "arn:aws:glue:us-east-2:123456789012:database/analytics_db",
         "TagsToAdd": {"Team": "platform"}},
    )]


@pytest.mark.parametrize("event_name,response,request_params,expected_arn_suffix", [
    ("CreateCrawler", {}, {"name": "my-crawler"}, "crawler/my-crawler"),
    ("CreateJob", {"name": "my-job"}, {"name": "my-job"}, "job/my-job"),
    ("CreateTrigger", {"name": "my-trigger"}, {}, "trigger/my-trigger"),
    ("CreateWorkflow", {"name": "my-flow"}, {}, "workflow/my-flow"),
    ("CreateSession", {"session": {"id": "sess-1"}}, {}, "session/sess-1"),
    ("CreateUsageProfile", {"name": "my-profile"}, {}, "usageProfile/my-profile"),
])
def test_glue_resource_types_resolve_their_own_arn(
        fake_boto3, event_name, response, request_params, expected_arn_suffix):
    event = _event("aws.glue", "glue.amazonaws.com", event_name,
                   response, request_params)
    lambda_handler.main(event, None)

    assert fake_boto3.calls[0][0] == "glue"
    assert fake_boto3.calls[0][2]["ResourceArn"] == (
        "arn:aws:glue:us-east-2:123456789012:" + expected_arn_suffix)


def test_glue_job_falls_back_to_the_request_name(fake_boto3):
    """CreateJob echoes the name in responseElements, but an empty body must
    still resolve from requestParameters."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateJob",
                   {}, {"name": "my-job"})
    lambda_handler.main(event, None)
    assert fake_boto3.calls[0][2]["ResourceArn"].endswith("job/my-job")


def test_glue_ml_transform_uses_the_transform_id_not_the_name(fake_boto3):
    """An ML transform's ARN is mlTransform/<TransformId> -- the generated ID,
    not the caller's chosen name. Building it from the name would produce an ARN
    that points at nothing."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateMLTransform",
                   {"transformId": "tfm-abc123"}, {"name": "my-transform"})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "glue", "tag_resource",
        {"ResourceArn": "arn:aws:glue:us-east-2:123456789012:mlTransform/tfm-abc123",
         "TagsToAdd": {"Team": "platform"}},
    )]


def test_glue_ml_transform_without_an_id_tags_nothing(fake_boto3):
    """No transform ID means no correct ARN is derivable, so the handler must
    no-op rather than fall back to the name."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateMLTransform",
                   {}, {"name": "my-transform"})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_glue_dev_endpoint_creation_is_a_no_op(fake_boto3):
    """devEndpoint is a taggable, billed Glue type that is still left uncovered:
    AWS has retired dev endpoints, so the event can no longer occur. Asserting
    the no-op keeps the omission deliberate rather than forgotten."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateDevEndpoint",
                   {}, {"endpointName": "my-ep"})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_glue_table_creation_is_a_no_op(fake_boto3):
    """glue:TagResource rejects a table/<db>/<table> ARN outright
    (InvalidInputException) -- the Data Catalog only tags down to the database
    level. A Glue CreateTable event reaching this Lambda must resolve no ARN
    rather than build one the tag API will refuse."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateTable",
                   {}, {"databaseName": "analytics_db",
                        "tableInput": {"name": "events"}})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_glue_connection_creation_is_a_no_op(fake_boto3):
    """Tagging a connection ARN makes Glue run an existence check needing
    glue:GetConnection, which can return the connection's plaintext PASSWORD.
    Connections are therefore out of scope -- a CreateConnection event must
    resolve no ARN so the role never needs that grant."""
    event = _event("aws.glue", "glue.amazonaws.com", "CreateConnection",
                   {}, {"connectionInput": {"name": "my-conn"}})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_athena_workgroup_tagged_with_uppercase_tag_list(fake_boto3):
    event = _event("aws.athena", "athena.amazonaws.com", "CreateWorkGroup",
                   {}, {"name": "analytics-wg"})
    lambda_handler.main(event, None)

    assert fake_boto3.calls == [(
        "athena", "tag_resource",
        {"ResourceARN": "arn:aws:athena:us-east-2:123456789012:workgroup/analytics-wg",
         "Tags": [{"Key": "Team", "Value": "platform"}]},
    )]


@pytest.mark.parametrize("event_name,expected_arn_suffix", [
    ("CreateDataCatalog", "datacatalog/my-catalog"),
    ("CreateCapacityReservation", "capacity-reservation/my-catalog"),
])
def test_athena_other_resource_types(fake_boto3, event_name, expected_arn_suffix):
    event = _event("aws.athena", "athena.amazonaws.com", event_name,
                   {}, {"name": "my-catalog"})
    lambda_handler.main(event, None)
    assert fake_boto3.calls[0][2]["ResourceARN"] == (
        "arn:aws:athena:us-east-2:123456789012:" + expected_arn_suffix)


def test_athena_workgroup_casing_is_not_confused_with_redshift_serverless(fake_boto3):
    """Athena emits CreateWork*G*roup; Redshift Serverless emits CreateWorkgroup.
    Athena must not act on the Redshift spelling."""
    event = _event("aws.athena", "athena.amazonaws.com", "CreateWorkgroup",
                   {}, {"name": "analytics-wg"})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_glue_and_athena_events_without_a_name_tag_nothing(fake_boto3):
    for source, event_source, event_name in (
            ("aws.glue", "glue.amazonaws.com", "CreateCrawler"),
            ("aws.athena", "athena.amazonaws.com", "CreateWorkGroup")):
        event = _event(source, event_source, event_name, {}, {})
        lambda_handler.main(event, None)
    assert fake_boto3.calls == []


def test_redshift_serverless_ignores_workgroup_events(fake_boto3):
    """CreateWorkgroup is routed to its own state machine (PART 7); if it ever
    reaches this Lambda it must be a no-op, not a mis-tag."""
    event = _event("aws.redshift-serverless", "redshift-serverless.amazonaws.com",
                   "CreateWorkgroup",
                   {"workgroup": {"workgroupName": "wg-1", "status": "CREATING"}})
    lambda_handler.main(event, None)
    assert fake_boto3.calls == []
