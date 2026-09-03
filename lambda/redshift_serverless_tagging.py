# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Redshift Serverless *workgroup* tagging steps for the Step Functions workflow.

Why only the workgroup
----------------------
Redshift Serverless has two resources with very different timing. A **namespace**
is AVAILABLE as soon as CreateNamespace returns, so it is tagged immediately by
the generic Lambda (see lambda-handler.aws_redshift_serverless). A **workgroup**
spends minutes in CREATING before reaching AVAILABLE, and the documentation does
not guarantee tagging succeeds during provisioning -- so it takes the same
"wait until ready" route as every other slow resource in this sample.

The two are split by eventName across two EventBridge rules
(CreateNamespace -> generic Lambda, CreateWorkgroup -> this workflow), so they
never collide despite sharing the aws.redshift-serverless source.

This single Lambda serves two state-machine steps, dispatched on ``action``:

  - prepare (default): {workgroupName, tagList, waitSeconds}
  - check:             {ready, status, arn}
"""

import os
import json
import random

import boto3


def _extract_workgroup_name(detail):
    response = detail.get("responseElements") or {}
    request = detail.get("requestParameters") or {}
    workgroup = response.get("workgroup") or {}
    name = workgroup.get("workgroupName") or request.get("workgroupName")
    if not name:
        raise ValueError(
            "Could not determine workgroupName from event. eventName="
            f"{detail.get('eventName')}")
    return name


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
    workgroup_name = _extract_workgroup_name(detail)
    print(f"resolved workgroupName: {workgroup_name}")

    res_tags = json.loads(os.environ["tags"])

    if os.environ.get("identityRecording", "false") == "true":
        user_id, role_id = _get_identity(detail)
        if role_id is not None:
            res_tags["roleId"] = role_id
        res_tags["userId"] = user_id

    return {
        "workgroupName": workgroup_name,
        # The Step Functions AWS SDK integration takes PascalCase {Key, Value}
        # objects here, even though boto3's redshift-serverless client takes
        # lower-case {key, value}. Do not "fix" this to match boto3.
        "tagList": [{"Key": str(k), "Value": str(v)}
                    for k, v in res_tags.items()],
        # nosec B311 - non-cryptographic jitter only; not a security control.
        "waitSeconds": random.randint(60, 120),  # nosec B311
    }


def _check(event):
    """Loop state: report whether the workgroup is AVAILABLE yet."""
    workgroup_name = event["workgroupName"]
    resp = boto3.client("redshift-serverless").get_workgroup(
        workgroupName=workgroup_name)
    workgroup = resp.get("workgroup") or {}
    status = workgroup.get("status")
    arn = workgroup.get("workgroupArn")
    ready = status == "AVAILABLE"
    print(f"workgroup {workgroup_name}: status={status} ready={ready} arn={arn}")
    return {"ready": ready, "status": status, "arn": arn}


def main(event, context):
    print(f"input event is: {json.dumps(event, default=str)}")
    action = event.get("action", "prepare")
    if action == "check":
        return _check(event)
    return _prepare(event)
