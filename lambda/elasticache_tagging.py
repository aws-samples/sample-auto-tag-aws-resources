# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ElastiCache tagging steps for the Step Functions polling workflow.

Why a polling workflow
----------------------
An ElastiCache cache cluster / replication group / serverless cache can take a
long time to provision -- especially during batch creation, where the
provisioning queue backs up -- and it rejects tagging until it reaches the
``available`` state. Tagging synchronously from the generic Lambda (with a
boto3 waiter) times out under load.

ElastiCache *does* emit "...Complete" lifecycle events, but only via Amazon SNS
and only when the cluster was created with a ``NotificationTopicArn``. Because
we cannot control the creation path of every ElastiCache resource, we cannot
rely on those notifications. Instead this workflow is triggered by the
CloudTrail ``Create*`` event (always present) and *polls* Describe* until the
resource is ``available``, then tags it.

This module is a single Lambda used in two state-machine steps, dispatched on
the ``action`` field of the input:

  - prepare (default): parse the raw CloudTrail event into
        {resourceType, resourceId, tagList, waitSeconds}
  - check:             describe the resource and return
        {ready: bool, arn: str|None, status: str}

Both Amazon RDS-style identity recording semantics are preserved: the prepare
step runs off the CloudTrail event, which *does* carry userIdentity, so
``identityRecording`` still works for ElastiCache.
"""

import os
import json
import random

import boto3


# Map the CloudTrail eventName to a normalized resource type + how to read the
# resource id out of the event. ElastiCache returns ids in different places
# depending on the API, so we try responseElements first then requestParameters.
_EVENT_NAME_TO_TYPE = {
    "CreateCacheCluster": "cacheCluster",
    "CreateReplicationGroup": "replicationGroup",
    "CreateServerlessCache": "serverlessCache",
}


def _first(*values):
    """Return the first truthy value (used for response/request fallbacks)."""
    for v in values:
        if v:
            return v
    return None


def _extract_resource(detail):
    """Return (resourceType, resourceId) from a CloudTrail Create* event."""
    event_name = detail.get("eventName")
    resource_type = _EVENT_NAME_TO_TYPE.get(event_name)
    if resource_type is None:
        raise ValueError(f"Unsupported ElastiCache eventName: {event_name}")

    response = detail.get("responseElements") or {}
    request = detail.get("requestParameters") or {}

    if resource_type == "cacheCluster":
        resource_id = _first(response.get("cacheClusterId"),
                              request.get("cacheClusterId"))
    elif resource_type == "replicationGroup":
        resource_id = _first(response.get("replicationGroupId"),
                              request.get("replicationGroupId"))
    else:  # serverlessCache
        sc_resp = response.get("serverlessCache") or {}
        resource_id = _first(sc_resp.get("serverlessCacheName"),
                              response.get("serverlessCacheName"),
                              request.get("serverlessCacheName"))

    if not resource_id:
        raise ValueError(
            f"Could not determine resourceId for {resource_type} "
            f"from eventName={event_name}")
    return resource_type, resource_id


def _get_identity(detail):
    """Return (userId, roleId|None) parsed from userIdentity, mirroring the
    generic tagging Lambda."""
    user_identity = detail.get("userIdentity", {})
    arn = user_identity.get("arn", "")
    parts = arn.split("/")
    user_id = parts[-1] if parts else ""
    role_id = None
    if user_identity.get("type") == "AssumedRole" and len(parts) >= 2:
        role_id = parts[-2]
    return user_id, role_id


def _prepare(event):
    """First state: normalize the CloudTrail event into the workflow payload."""
    detail = event.get("detail", {})
    resource_type, resource_id = _extract_resource(detail)
    print(f"resolved ElastiCache resource: type={resource_type} id={resource_id}")

    res_tags = json.loads(os.environ["tags"])

    # The CloudTrail Create event carries userIdentity, so identityRecording
    # still works for ElastiCache (unlike the RDS service-event path).
    if os.environ.get("identityRecording", "false") == "true":
        user_id, role_id = _get_identity(detail)
        if role_id is not None:
            res_tags["roleId"] = role_id
        res_tags["userId"] = user_id

    tag_list = [{"Key": str(k), "Value": str(v)} for k, v in res_tags.items()]

    return {
        "resourceType": resource_type,
        "resourceId": resource_id,
        "tagList": tag_list,
        # Randomize the first wait so a batch of concurrent executions does not
        # hammer the Describe* APIs in lockstep (see add_retry jitter too).
        # nosec B311 - non-cryptographic jitter only; not used for any security purpose.
        "waitSeconds": random.randint(60, 120),  # nosec B311
    }


def _check(event):
    """Loop state: describe the resource and report whether it is available."""
    resource_type = event["resourceType"]
    resource_id = event["resourceId"]
    client = boto3.client("elasticache")

    if resource_type == "cacheCluster":
        resp = client.describe_cache_clusters(CacheClusterId=resource_id)
        item = resp["CacheClusters"][0]
        status = item.get("CacheClusterStatus")
        arn = item.get("ARN")
    elif resource_type == "replicationGroup":
        resp = client.describe_replication_groups(ReplicationGroupId=resource_id)
        item = resp["ReplicationGroups"][0]
        status = item.get("Status")
        arn = item.get("ARN")
    else:  # serverlessCache
        resp = client.describe_serverless_caches(ServerlessCacheName=resource_id)
        item = resp["ServerlessCaches"][0]
        status = item.get("Status")
        arn = item.get("ARN")

    ready = status == "available"
    print(f"{resource_type} {resource_id}: status={status} ready={ready} arn={arn}")
    return {"ready": ready, "status": status, "arn": arn}


def main(event, context):
    print(f"input event is: {json.dumps(event, default=str)}")
    action = event.get("action", "prepare")
    if action == "check":
        return _check(event)
    return _prepare(event)
