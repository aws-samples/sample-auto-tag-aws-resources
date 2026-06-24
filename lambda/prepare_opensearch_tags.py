# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Prepare step for the OpenSearch tagging Step Functions workflow.

This Lambda is the first state in the state machine. It receives the raw
EventBridge / CloudTrail event for an OpenSearch domain creation and returns a
small, normalized payload that the rest of the state machine consumes:

    {
        "domainName": "<the domain name>",
        "tagList": [ {"Key": "...", "Value": "..."}, ... ]
    }

It handles both API versions:
  - CreateDomain               (new, engine-agnostic API, 2021-01-01)
  - CreateElasticsearchDomain  (legacy Elasticsearch API)

Both share the same eventSource (es.amazonaws.com) and the same response shape
(responseElements.domainStatus.domainName), so a single extraction path works,
with defensive fallbacks for safety.
"""

import os
import json


def _extract_domain_name(event):
    """Pull the domain name out of the CloudTrail event, tolerant of shape."""
    detail = event.get("detail", {})

    # Preferred: the API response contains the created domain status.
    response_elements = detail.get("responseElements") or {}
    domain_status = response_elements.get("domainStatus") or {}
    domain_name = domain_status.get("domainName")
    if domain_name:
        return domain_name

    # Fallback: the request parameters carry the requested domain name.
    request_parameters = detail.get("requestParameters") or {}
    domain_name = request_parameters.get("domainName")
    if domain_name:
        return domain_name

    raise ValueError(
        "Could not determine domainName from event. eventName="
        f"{detail.get('eventName')}"
    )


def _get_identity(detail):
    """Return (userId, roleId|None) parsed from the userIdentity, mirroring the
    behaviour of the original generic tagging Lambda."""
    user_identity = detail.get("userIdentity", {})
    arn = user_identity.get("arn", "")
    parts = arn.split("/")
    user_id = parts[-1] if parts else ""

    role_id = None
    if user_identity.get("type") == "AssumedRole" and len(parts) >= 2:
        role_id = parts[-2]
    return user_id, role_id


def main(event, context):
    print(f"input event is: {json.dumps(event, default=str)}")

    detail = event.get("detail", {})
    domain_name = _extract_domain_name(event)
    print(f"resolved domainName: {domain_name}")

    # Base tags come from the deployment parameter (JSON string).
    res_tags = json.loads(os.environ["tags"])

    # Optionally record the requester identity, same semantics as the original.
    identity_recording = os.environ.get("identityRecording", "false")
    if identity_recording == "true":
        user_id, role_id = _get_identity(detail)
        if role_id is not None:
            res_tags["roleId"] = role_id
        res_tags["userId"] = user_id

    # OpenSearch AddTags expects a TagList of {Key, Value} objects.
    tag_list = [{"Key": str(k), "Value": str(v)} for k, v in res_tags.items()]
    print(f"prepared tagList: {tag_list}")

    return {
        "domainName": domain_name,
        "tagList": tag_list,
    }
