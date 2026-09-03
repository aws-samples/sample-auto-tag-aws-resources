# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Redshift provisioned-cluster tagging steps for the Step Functions workflow.

Why a polling workflow
----------------------
A provisioned cluster spends several minutes in ``creating`` before it reaches
``available``, and Redshift publishes no native EventBridge readiness event. So
the workflow is triggered by the CloudTrail ``CreateCluster`` event and polls
``DescribeClusters``.

Why prepare builds the ARN
--------------------------
``DescribeClusters`` returns no ARN field, so unlike the Kinesis and Redshift
Serverless workflows the ARN cannot be read off the describe response. It is
constructed here from the event's account and region instead, which is why
``check`` returns only {ready, status}.

This single Lambda serves two state-machine steps, dispatched on ``action``:

  - prepare (default): {clusterIdentifier, resourceArn, tagList, waitSeconds}
  - check:             {ready, status}
"""

import os
import json
import random

import boto3


# DescribeClusters returns no ARN, so it is built from the event.
# Known limitation: the partition is hard-coded to "aws", matching the ARN
# templates already used in lambda/lambda-handler.py. Deployments in the
# China or GovCloud partitions need this adjusted.
_CLUSTER_ARN_TEMPLATE = "arn:aws:redshift:{region}:{account}:cluster:{cluster_id}"


def _extract_cluster_identifier(detail):
    response = detail.get("responseElements") or {}
    request = detail.get("requestParameters") or {}
    cluster_id = (response.get("clusterIdentifier")
                  or request.get("clusterIdentifier"))
    if not cluster_id:
        raise ValueError(
            "Could not determine clusterIdentifier from event. eventName="
            f"{detail.get('eventName')}")
    return cluster_id


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
    cluster_id = _extract_cluster_identifier(detail)
    resource_arn = _CLUSTER_ARN_TEMPLATE.format(
        region=event["region"], account=event["account"], cluster_id=cluster_id)
    print(f"resolved cluster: id={cluster_id} arn={resource_arn}")

    res_tags = json.loads(os.environ["tags"])

    if os.environ.get("identityRecording", "false") == "true":
        user_id, role_id = _get_identity(detail)
        if role_id is not None:
            res_tags["roleId"] = role_id
        res_tags["userId"] = user_id

    return {
        "clusterIdentifier": cluster_id,
        "resourceArn": resource_arn,
        # redshift:CreateTags takes a list of {Key, Value} objects.
        "tagList": [{"Key": str(k), "Value": str(v)}
                    for k, v in res_tags.items()],
        # nosec B311 - non-cryptographic jitter only; not a security control.
        "waitSeconds": random.randint(60, 120),  # nosec B311
    }


def _check(event):
    """Loop state: report whether the cluster is available yet."""
    cluster_id = event["clusterIdentifier"]
    resp = boto3.client("redshift").describe_clusters(
        ClusterIdentifier=cluster_id)
    item = resp["Clusters"][0]
    status = item.get("ClusterStatus")
    ready = status == "available"
    print(f"cluster {cluster_id}: status={status} ready={ready}")
    return {"ready": ready, "status": status}


def main(event, context):
    print(f"input event is: {json.dumps(event, default=str)}")
    action = event.get("action", "prepare")
    if action == "check":
        return _check(event)
    return _prepare(event)
