# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Kinesis Data Streams tagging steps for the Step Functions polling workflow.

Why a polling workflow
----------------------
A stream is ``CREATING`` immediately after ``CreateStream`` and
``AddTagsToStream`` only succeeds once it is ``ACTIVE``. Kinesis publishes no
"stream is ready" event, so the workflow is triggered by the CloudTrail
``CreateStream`` event and polls ``DescribeStreamSummary``.

Two Kinesis quirks shape this module:

  - ``CreateStream`` returns an **empty body**, so ``responseElements`` is null
    and the stream name must come from ``requestParameters``.
  - ``AddTagsToStream`` takes a ``{key: value}`` **map**, not a ``TagList``, so
    prepare emits ``tagMap`` and the state machine passes it through with
    ``JsonPath.object_at``.

This single Lambda serves two state-machine steps, dispatched on the ``action``
field of its input:

  - prepare (default): {streamName, tagMap, waitSeconds}
  - check:             {ready, status, arn}
"""

import os
import json
import random

import boto3


def _extract_stream_name(detail):
    """Return the stream name; CreateStream leaves responseElements null."""
    request = detail.get("requestParameters") or {}
    response = detail.get("responseElements") or {}
    name = request.get("streamName") or response.get("streamName")
    if not name:
        raise ValueError(
            "Could not determine streamName from event. eventName="
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
    stream_name = _extract_stream_name(detail)
    print(f"resolved streamName: {stream_name}")

    res_tags = json.loads(os.environ["tags"])

    # The CloudTrail Create event carries userIdentity, so identityRecording
    # works on this path.
    if os.environ.get("identityRecording", "false") == "true":
        user_id, role_id = _get_identity(detail)
        if role_id is not None:
            res_tags["roleId"] = role_id
        res_tags["userId"] = user_id

    return {
        "streamName": stream_name,
        # AddTagsToStream takes a {key: value} map, not a TagList.
        "tagMap": {str(k): str(v) for k, v in res_tags.items()},
        # Randomize the retry wait so a batch of concurrent executions does not
        # poll in lockstep. A stream normally goes ACTIVE within seconds, so
        # this window is shorter than the ElastiCache one.
        # nosec B311 - non-cryptographic jitter only; not a security control.
        "waitSeconds": random.randint(15, 45),  # nosec B311
    }


def _check(event):
    """Loop state: report whether the stream is ACTIVE yet."""
    stream_name = event["streamName"]
    resp = boto3.client("kinesis").describe_stream_summary(
        StreamName=stream_name)
    summary = resp["StreamDescriptionSummary"]
    status = summary.get("StreamStatus")
    arn = summary.get("StreamARN")
    ready = status == "ACTIVE"
    print(f"stream {stream_name}: status={status} ready={ready} arn={arn}")
    return {"ready": ready, "status": status, "arn": arn}


def main(event, context):
    print(f"input event is: {json.dumps(event, default=str)}")
    action = event.get("action", "prepare")
    if action == "check":
        return _check(event)
    return _prepare(event)
