#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from auto_tag_resource.auto_tag_resource_stack import AutoTagResourceStack


app = cdk.App()

# Run cdk_nag's AwsSolutionsChecks on synth. Findings that are inherent to this
# tagging-automation sample (e.g. wildcard resources required to tag
# deploy-time-unknown ARNs) are suppressed with documented justifications in
# the stack itself; see THREAT_MODEL.md T1/T8.
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

AutoTagResourceStack(app, "AutoTagResourceStack",
    # If you don't specify 'env', this stack will be environment-agnostic.
    # Account/Region-dependent features and context lookups will not work,
    # but a single synthesized template can be deployed anywhere.

    # Uncomment the next line to specialize this stack for the AWS Account
    # and Region that are implied by the current CLI configuration.

    #env=cdk.Environment(account=os.getenv('CDK_DEFAULT_ACCOUNT'), region=os.getenv('CDK_DEFAULT_REGION')),

    # Uncomment the next line if you know exactly what Account and Region you
    # want to deploy the stack to. */

    #env=cdk.Environment(account='123456789012', region='us-east-1'),

    # For more information, see https://docs.aws.amazon.com/cdk/latest/guide/environments.html
    )

app.synth()
