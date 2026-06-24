# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tag RDS / Aurora resources from the RDS *service* event.

Why this exists
---------------
RDS and Aurora resources are NOT immediately taggable right after the
CloudTrail ``CreateDBInstance`` / ``CreateDBCluster`` API call -- the resource
is still being provisioned, and during batch creation the provisioning queue
can take a long time. The original design tagged synchronously from the
generic Lambda (optionally with a boto3 waiter), which times out under load.

Instead, this Lambda reacts to the RDS *service event* that AWS emits once the
resource is available:

  - ``RDS-EVENT-0170`` -- "DB cluster created"  (Aurora / Multi-AZ clusters)
  - ``RDS-EVENT-0005`` -- "DB instance created" (all DB instances, including
                          the writer/reader instances of an Aurora cluster)

These events arrive on the native ``aws.rds`` EventBridge source (no SNS topic
or subscription required) with this shape::

    {
      "source": "aws.rds",
      "detail-type": "RDS DB Cluster Event" | "RDS DB Instance Event",
      "detail": {
        "EventID": "RDS-EVENT-0170",
        "SourceArn": "arn:aws:rds:<region>:<acct>:cluster:<name>",
        "SourceIdentifier": "<name>",
        ...
      }
    }

``detail.SourceArn`` is the ARN of the just-created resource, so we can tag it
directly with no Describe call.

Identity recording
-------------------
The RDS service event does NOT carry a ``userIdentity`` block (it is emitted by
the RDS service, not by the API caller), so the ``identityRecording`` feature
cannot record the requester for RDS/Aurora. Base tags from the ``tags``
deployment parameter are always applied.

This single handler covers both Amazon RDS and Amazon Aurora. Amazon
DocumentDB also rides the ``aws.rds`` event stream and is covered as well, but
the EventBridge rule (see the CDK stack) controls exactly which event sources
reach this function.
"""

import os
import json

import boto3


def _arn_from_event(detail):
    """Return the resource ARN carried by the RDS service event."""
    arn = detail.get("SourceArn")
    if not arn:
        raise ValueError(
            "RDS service event has no SourceArn; EventID="
            f"{detail.get('EventID')} SourceIdentifier={detail.get('SourceIdentifier')}"
        )
    return arn


def main(event, context):
    print(f"input event is: {json.dumps(event, default=str)}")

    detail = event.get("detail", {})
    event_id = detail.get("EventID")
    arn = _arn_from_event(detail)
    print(f"tagging RDS/Aurora resource: arn={arn} eventId={event_id}")

    # Base tags come from the deployment parameter (JSON string).
    res_tags = json.loads(os.environ["tags"])

    # The RDS service event carries no userIdentity, so identityRecording does
    # not apply here. We surface a log line if it was requested for visibility.
    if os.environ.get("identityRecording", "false") == "true":
        print("identityRecording is enabled but the RDS service event carries "
              "no userIdentity; skipping requester tags for this resource.")

    tags = [{"Key": str(k), "Value": str(v)} for k, v in res_tags.items()]

    # rds:AddTagsToResource works for RDS DB instances, Aurora DB clusters, and
    # Aurora DB instances alike (the ARN type differentiates them).
    boto3.client("rds").add_tags_to_resource(
        ResourceName=arn,
        Tags=tags,
    )
    print(f"tagged {arn} with {tags}")

    return {
        "statusCode": 200,
        "body": json.dumps(f"Tagged RDS/Aurora resource {arn}"),
    }
