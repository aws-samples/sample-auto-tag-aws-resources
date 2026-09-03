# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from aws_cdk import (
    CfnParameter,
    Duration,
    RemovalPolicy,
    Stack,
    Aws,
    aws_iam as _iam,
    aws_events as _events,
    aws_events_targets as _targets,
    aws_lambda as _lambda,
    aws_logs as _logs,
    aws_stepfunctions as _sfn,
    aws_stepfunctions_tasks as _tasks,
)
from cdk_nag import NagSuppressions
from constructs import Construct


class AutoTagResourceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # Parameters
        # ------------------------------------------------------------------
        tags = CfnParameter(self, "tags", type="String",
                            description="tag name and value with json format.")
        identityRecording = CfnParameter(self, "identityRecording", type="String", default="false",
                            description="Defines if the tool records the requester identity as a tag.")

        # ==================================================================
        # PART 1: Generic resource tagging Lambda (all services EXCEPT OpenSearch)
        # OpenSearch is handled separately by a Step Functions workflow (PART 2)
        # because an OpenSearch domain rejects any tag/config change while it
        # is still in the "Processing" state right after CreateDomain.
        # ==================================================================
        lambda_role = _iam.Role(self, "lambda_role",
            role_name=f"resource-tagging-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))

        lambda_role.add_managed_policy(_iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        # Least-privilege: every action below is either a tag-write the handler
        # actually calls or a read-only permission the Resource Groups Tagging
        # API / boto3 waiters require to tag the corresponding resource type.
        # Unused grants were removed during threat modeling (see THREAT_MODEL.md
        # T2/T8): tag:UntagResources (mutating, never called), cloudformation:*
        # (never called), and ec2:DescribeNatGateways/DescribeInternetGateways
        # (no handler path reaches them).
        lambda_role.add_to_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            resources=["*"],
            actions=["dynamodb:TagResource", "dynamodb:DescribeTable", "lambda:TagResource", "lambda:ListTags", "s3:GetBucketTagging", "s3:PutBucketTagging",
            "ec2:CreateTags", "ec2:DescribeVolumes",
            "sns:TagResource", "sqs:ListQueueTags", "sqs:TagQueue", "kms:ListResourceTags", "kms:TagResource", "elasticfilesystem:TagResource",
            "elasticfilesystem:CreateTags", "elasticfilesystem:DescribeTags", "elasticloadbalancing:AddTags", "logs:CreateLogGroup", "logs:CreateLogStream",
            "logs:PutLogEvents", "tag:getResources", "tag:getTagKeys", "tag:getTagValues", "tag:TagResources",
            "GameLift:TagResource", "logs:TagLogGroup",
            "kafka:TagResource", "eks:TagResource", "ecs:TagResource",
            "kinesisanalytics:TagResource", "redshift-serverless:TagResource"]
        ))

        tagging_function = _lambda.Function(self, "resource_tagging_automation_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(600),
                                    handler="lambda-handler.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="resource-tagging-automation-function",
                                    role=lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        # EventBridge rule for all generic services.
        # Intentionally NOT handled here (each waits for the resource to finish
        # provisioning before it can be tagged, so they are handled out of band):
        #   - OpenSearch / aws.es            -> Step Functions workflow (PART 2)
        #   - RDS & Aurora / aws.rds         -> RDS service-event Lambda  (PART 3)
        #   - ElastiCache / aws.elasticache  -> Step Functions workflow  (PART 4)
        #   - Kinesis / Redshift / Redshift Serverless workgroup
        #                                    -> Step Functions workflows (PART 5/6/7)
        # Note: "CreateCluster" is used by MSK, EKS and ECS alike. EventBridge
        # ANDs source/eventSource/eventName, and the Lambda dispatches on
        # `source`, so the three cannot cross over. Redshift's CreateCluster is
        # excluded because redshift.amazonaws.com is not in eventSource here.
        _eventRule = _events.Rule(self, "resource-tagging-automation-rule",
                        rule_name="resource-tagging-automation-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.ec2", "aws.elasticloadbalancing", "aws.lambda", "aws.s3", "aws.dynamodb", "aws.elasticfilesystem", "aws.sqs", "aws.sns", "aws.kms", "aws.gamelift", "aws.logs", "aws.kafka", "aws.eks", "aws.ecs", "aws.kinesisanalytics", "aws.redshift-serverless"],
                            detail_type=["AWS API Call via CloudTrail"],
                            detail={
                                "eventSource": ["ec2.amazonaws.com", "elasticloadbalancing.amazonaws.com", "s3.amazonaws.com", "lambda.amazonaws.com", "dynamodb.amazonaws.com", "elasticfilesystem.amazonaws.com", "sqs.amazonaws.com", "sns.amazonaws.com", "kms.amazonaws.com", "gamelift.amazonaws.com", "logs.amazonaws.com", "kafka.amazonaws.com", "eks.amazonaws.com", "ecs.amazonaws.com", "kinesisanalytics.amazonaws.com", "redshift-serverless.amazonaws.com"],
                                "eventName": ["RunInstances", "CreateFunction20150331", "CreateBucket", "CreateTable", "CreateVolume", "CreateLoadBalancer", "CreateMountTarget", "CreateQueue", "CreateTopic", "CreateKey", "CreateFleet", "CreateLogGroup", "CreateCluster", "CreateClusterV2", "CreateService", "CreateApplication", "CreateNamespace"],
                                # CloudTrail records *rejected* API calls too, and
                                # a rejected Create* produced no resource to tag.
                                # Without this filter a failed call still triggers
                                # tagging: the generic Lambda would try to tag an
                                # ARN that does not exist, and the polling
                                # workflows would poll a resource that will never
                                # appear until they time out. Every CloudTrail rule
                                # below carries the same filter.
                                "errorCode": [{"exists": False}]
                            }
                        )
                    )
        _eventRule.add_target(_targets.LambdaFunction(tagging_function, retry_attempts=2))

        # ==================================================================
        # PART 3: RDS / Aurora tagging from the RDS service event
        # A DB instance/cluster is not taggable right after the CloudTrail
        # CreateDB* call (still provisioning), and batch creation makes any
        # synchronous waiter time out. Instead we react to the RDS service
        # event that AWS emits once the resource is available:
        #   RDS-EVENT-0170 "DB cluster created"  (Aurora / Multi-AZ clusters)
        #   RDS-EVENT-0005 "DB instance created" (all DB instances)
        # These arrive on the native aws.rds EventBridge source (no SNS setup),
        # and detail.SourceArn is the ARN to tag -- no Describe needed.
        # ==================================================================
        rds_lambda_role = _iam.Role(self, "rds_tagging_lambda_role",
            role_name=f"rds-tagging-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))
        rds_lambda_role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        rds_lambda_role.add_to_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            resources=["*"],
            actions=["rds:AddTagsToResource", "rds:DescribeDBInstances", "rds:DescribeDBClusters"]
        ))

        rds_tagging_function = _lambda.Function(self, "rds_tagging_on_ready_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(60),
                                    handler="tag_rds_on_ready.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="rds-tagging-on-ready-function",
                                    role=rds_lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        # RDS service event: fire when a DB cluster or DB instance is *created*
        # (i.e. available). EventID filtering keeps us off every other RDS event.
        _rdsReadyRule = _events.Rule(self, "rds-tagging-on-ready-rule",
                        rule_name="rds-tagging-on-ready-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.rds"],
                            detail_type=["RDS DB Cluster Event", "RDS DB Instance Event"],
                            detail={
                                # RDS-EVENT-0170: DB cluster created (Aurora)
                                # RDS-EVENT-0005: DB instance created
                                "EventID": ["RDS-EVENT-0170", "RDS-EVENT-0005"]
                            }
                        )
                    )
        _rdsReadyRule.add_target(_targets.LambdaFunction(rds_tagging_function, retry_attempts=2))

        # ==================================================================
        # PART 2: OpenSearch tagging via Step Functions
        # Flow: EventBridge (CreateDomain / CreateElasticsearchDomain)
        #        -> Step Functions state machine
        #           PrepareTags (Lambda) -> DescribeDomain (loop until ready) -> AddTags
        # ==================================================================

        # --- 2.1 Prepare Lambda: parses the event and builds the TagList ---
        prepare_lambda_role = _iam.Role(self, "opensearch_prepare_lambda_role",
            role_name=f"opensearch-tagging-prepare-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))
        prepare_lambda_role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))

        prepare_function = _lambda.Function(self, "opensearch_tagging_prepare_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(60),
                                    handler="prepare_opensearch_tags.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="opensearch-tagging-prepare-function",
                                    role=prepare_lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        # --- 2.2 State machine log group ---
        sfn_log_group = _logs.LogGroup(self, "opensearch_tagging_logs",
            log_group_name="/aws/vendedlogs/states/opensearch-tagging",
            retention=_logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY)

        # --- 2.3 State machine steps ---
        # Step 1: prepare input -> {domainName, tagList}
        prepare_task = _tasks.LambdaInvoke(self, "PrepareTags",
            lambda_function=prepare_function,
            payload_response_only=True)

        # Step 2: describe the domain (poll). IAM namespace for OpenSearch is "es:".
        describe_task = _tasks.CallAwsService(self, "DescribeDomain",
            service="opensearch",
            action="describeDomain",
            iam_action="es:DescribeDomain",
            iam_resources=["*"],
            parameters={
                "DomainName": _sfn.JsonPath.string_at("$.domainName")
            },
            result_path="$.describe")
        describe_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(15),
            max_attempts=3,
            backoff_rate=2.0)

        # Step 3: wait between polls
        wait_task = _sfn.Wait(self, "WaitForDomain",
            time=_sfn.WaitTime.duration(Duration.seconds(60)))

        # Step 4: add tags once the domain is no longer Processing
        add_tags_task = _tasks.CallAwsService(self, "AddTags",
            service="opensearch",
            action="addTags",
            iam_action="es:AddTags",
            iam_resources=["*"],
            parameters={
                "Arn": _sfn.JsonPath.string_at("$.describe.DomainStatus.Arn"),
                "TagList": _sfn.JsonPath.list_at("$.tagList")
            },
            result_path=_sfn.JsonPath.DISCARD)
        # Guard against a race where the domain flips back to Processing.
        add_tags_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(30),
            max_attempts=5,
            backoff_rate=2.0)

        succeed_state = _sfn.Succeed(self, "TaggingComplete")
        fail_state = _sfn.Fail(self, "TaggingFailed",
            cause="Failed to tag the OpenSearch domain",
            error="OpenSearchTaggingError")

        # Choice: is the domain ready (Processing == false)?
        is_ready_choice = _sfn.Choice(self, "IsDomainReady")
        is_ready_choice.when(
            _sfn.Condition.boolean_equals("$.describe.DomainStatus.Processing", False),
            add_tags_task.next(succeed_state))
        is_ready_choice.otherwise(wait_task.next(describe_task))

        # Surface a hard failure if AddTags exhausts retries.
        add_tags_task.add_catch(fail_state, errors=["States.ALL"])

        definition = prepare_task.next(describe_task).next(is_ready_choice)

        state_machine = _sfn.StateMachine(self, "OpenSearchTaggingStateMachine",
            state_machine_name="opensearch-tagging-state-machine",
            definition_body=_sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.hours(2),
            logs=_sfn.LogOptions(
                destination=sfn_log_group,
                level=_sfn.LogLevel.ALL))

        # --- 2.4 EventBridge rule: OpenSearch domain creation (new + legacy API) ---
        _opensearchRule = _events.Rule(self, "opensearch-tagging-rule",
                        rule_name="opensearch-tagging-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.es"],
                            detail_type=["AWS API Call via CloudTrail"],
                            detail={
                                "eventSource": ["es.amazonaws.com"],
                                # CreateDomain        -> new engine-agnostic API (2021-01-01)
                                # CreateElasticsearchDomain -> legacy Elasticsearch API
                                "eventName": ["CreateDomain", "CreateElasticsearchDomain"],
                                "errorCode": [{"exists": False}]
                            }
                        )
                    )
        _opensearchRule.add_target(_targets.SfnStateMachine(state_machine))

        # ==================================================================
        # PART 4: ElastiCache tagging via Step Functions (polling)
        # ElastiCache can take a long time to provision and rejects tagging
        # until it is "available". Its "...Complete" lifecycle events are only
        # delivered via SNS and only when the resource was created with a
        # NotificationTopicArn -- which we cannot guarantee across all creation
        # paths. So we trigger off the CloudTrail Create* event (always
        # present) and poll Describe* until Status == available, then AddTags.
        # The CloudTrail event carries userIdentity, so identityRecording still
        # works here (unlike the RDS service-event path).
        # ==================================================================

        # --- 4.1 Lambda: used for both the prepare and check (poll) steps ---
        ec_lambda_role = _iam.Role(self, "elasticache_tagging_lambda_role",
            role_name=f"elasticache-tagging-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))
        ec_lambda_role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        ec_lambda_role.add_to_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            resources=["*"],
            actions=["elasticache:DescribeCacheClusters", "elasticache:DescribeReplicationGroups",
                     "elasticache:DescribeServerlessCaches"]
        ))

        ec_function = _lambda.Function(self, "elasticache_tagging_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(60),
                                    handler="elasticache_tagging.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="elasticache-tagging-function",
                                    role=ec_lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        # --- 4.2 State machine log group ---
        ec_sfn_log_group = _logs.LogGroup(self, "elasticache_tagging_logs",
            log_group_name="/aws/vendedlogs/states/elasticache-tagging",
            retention=_logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY)

        # --- 4.3 State machine steps ---
        # Step 1: prepare -> {resourceType, resourceId, tagList, waitSeconds}
        ec_prepare_task = _tasks.LambdaInvoke(self, "ECPrepareTags",
            lambda_function=ec_function,
            payload_response_only=True)

        # Step 2: wait. The first wait is randomized per execution (waitSeconds
        # from prepare) to spread Describe* calls across a batch; subsequent
        # waits reuse the same value.
        ec_wait_task = _sfn.Wait(self, "ECWaitForReady",
            time=_sfn.WaitTime.seconds_path("$.waitSeconds"))

        # Step 3: check -> {ready, status, arn}. We inject action=check and keep
        # the prepared fields so the loop can re-check.
        ec_check_task = _tasks.LambdaInvoke(self, "ECCheckReady",
            lambda_function=ec_function,
            payload=_sfn.TaskInput.from_object({
                "action": "check",
                "resourceType": _sfn.JsonPath.string_at("$.resourceType"),
                "resourceId": _sfn.JsonPath.string_at("$.resourceId"),
            }),
            result_path="$.check",
            payload_response_only=True)
        ec_check_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(15),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        # Step 4: add tags once available. ElastiCache AddTagsToResource takes
        # the resource ARN and a TagList of {Key, Value}.
        ec_add_tags_task = _tasks.CallAwsService(self, "ECAddTags",
            service="elasticache",
            action="addTagsToResource",
            iam_action="elasticache:AddTagsToResource",
            iam_resources=["*"],
            parameters={
                "ResourceName": _sfn.JsonPath.string_at("$.check.arn"),
                "Tags": _sfn.JsonPath.list_at("$.tagList")
            },
            result_path=_sfn.JsonPath.DISCARD)
        ec_add_tags_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(30),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        ec_succeed_state = _sfn.Succeed(self, "ECTaggingComplete")
        ec_fail_state = _sfn.Fail(self, "ECTaggingFailed",
            cause="Failed to tag the ElastiCache resource",
            error="ElastiCacheTaggingError")

        # Choice: is the resource available?
        ec_is_ready_choice = _sfn.Choice(self, "ECIsResourceReady")
        ec_is_ready_choice.when(
            _sfn.Condition.string_equals("$.check.status", "available"),
            ec_add_tags_task.next(ec_succeed_state))
        ec_is_ready_choice.otherwise(ec_wait_task.next(ec_check_task))

        ec_add_tags_task.add_catch(ec_fail_state, errors=["States.ALL"])

        # prepare -> check -> ready? (yes: addTags / no: wait -> check)
        # The Wait state appears only in the loop-back branch (a Wait can have
        # just one "next"); the first check runs immediately, like the
        # OpenSearch state machine.
        ec_definition = ec_prepare_task.next(ec_check_task).next(ec_is_ready_choice)

        ec_state_machine = _sfn.StateMachine(self, "ElastiCacheTaggingStateMachine",
            state_machine_name="elasticache-tagging-state-machine",
            definition_body=_sfn.DefinitionBody.from_chainable(ec_definition),
            timeout=Duration.hours(2),
            logs=_sfn.LogOptions(
                destination=ec_sfn_log_group,
                level=_sfn.LogLevel.ALL))

        # --- 4.4 EventBridge rule: ElastiCache resource creation (CloudTrail) ---
        _elasticacheRule = _events.Rule(self, "elasticache-tagging-rule",
                        rule_name="elasticache-tagging-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.elasticache"],
                            detail_type=["AWS API Call via CloudTrail"],
                            detail={
                                "eventSource": ["elasticache.amazonaws.com"],
                                "eventName": ["CreateCacheCluster", "CreateReplicationGroup", "CreateServerlessCache"],
                                "errorCode": [{"exists": False}]
                            }
                        )
                    )
        _elasticacheRule.add_target(_targets.SfnStateMachine(ec_state_machine))

        # ==================================================================
        # PART 5: Kinesis Data Streams tagging via Step Functions (polling)
        # A stream is CREATING right after CreateStream and AddTagsToStream only
        # succeeds once it is ACTIVE. Kinesis emits no readiness event, so the
        # workflow is triggered by the CloudTrail Create event and polls
        # DescribeStreamSummary -- which is also where the stream ARN comes from,
        # since CreateStream returns an empty body.
        # ==================================================================
        kinesis_lambda_role = _iam.Role(self, "kinesis_tagging_lambda_role",
            role_name=f"kinesis-tagging-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))
        kinesis_lambda_role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        kinesis_lambda_role.add_to_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            resources=["*"],
            actions=["kinesis:DescribeStreamSummary"]
        ))

        kinesis_function = _lambda.Function(self, "kinesis_tagging_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(60),
                                    handler="kinesis_tagging.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="kinesis-tagging-function",
                                    role=kinesis_lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        kinesis_sfn_log_group = _logs.LogGroup(self, "kinesis_tagging_logs",
            log_group_name="/aws/vendedlogs/states/kinesis-tagging",
            retention=_logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY)

        kinesis_prepare_task = _tasks.LambdaInvoke(self, "KinesisPrepareTags",
            lambda_function=kinesis_function,
            payload_response_only=True)

        kinesis_wait_task = _sfn.Wait(self, "KinesisWaitForReady",
            time=_sfn.WaitTime.seconds_path("$.waitSeconds"))

        kinesis_check_task = _tasks.LambdaInvoke(self, "KinesisCheckReady",
            lambda_function=kinesis_function,
            payload=_sfn.TaskInput.from_object({
                "action": "check",
                "streamName": _sfn.JsonPath.string_at("$.streamName"),
            }),
            result_path="$.check",
            payload_response_only=True)
        kinesis_check_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(15),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        # AddTagsToStream takes a {key: value} map, so the prepared tagMap object
        # is passed through whole with object_at -- not a TagList like the other
        # workflows.
        kinesis_add_tags_task = _tasks.CallAwsService(self, "KinesisAddTags",
            service="kinesis",
            action="addTagsToStream",
            iam_action="kinesis:AddTagsToStream",
            iam_resources=["*"],
            parameters={
                "StreamName": _sfn.JsonPath.string_at("$.streamName"),
                "Tags": _sfn.JsonPath.object_at("$.tagMap")
            },
            result_path=_sfn.JsonPath.DISCARD)
        kinesis_add_tags_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(30),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        kinesis_succeed_state = _sfn.Succeed(self, "KinesisTaggingComplete")
        kinesis_fail_state = _sfn.Fail(self, "KinesisTaggingFailed",
            cause="Failed to tag the Kinesis data stream",
            error="KinesisTaggingError")

        kinesis_is_ready_choice = _sfn.Choice(self, "KinesisIsStreamReady")
        kinesis_is_ready_choice.when(
            _sfn.Condition.string_equals("$.check.status", "ACTIVE"),
            kinesis_add_tags_task.next(kinesis_succeed_state))
        kinesis_is_ready_choice.otherwise(
            kinesis_wait_task.next(kinesis_check_task))

        kinesis_add_tags_task.add_catch(kinesis_fail_state, errors=["States.ALL"])

        kinesis_definition = kinesis_prepare_task.next(kinesis_check_task).next(
            kinesis_is_ready_choice)

        kinesis_state_machine = _sfn.StateMachine(self, "KinesisTaggingStateMachine",
            state_machine_name="kinesis-tagging-state-machine",
            definition_body=_sfn.DefinitionBody.from_chainable(kinesis_definition),
            timeout=Duration.hours(2),
            logs=_sfn.LogOptions(
                destination=kinesis_sfn_log_group,
                level=_sfn.LogLevel.ALL))

        # --- 5.4 EventBridge rule: Kinesis stream creation (CloudTrail) ---
        _kinesisRule = _events.Rule(self, "kinesis-tagging-rule",
                        rule_name="kinesis-tagging-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.kinesis"],
                            detail_type=["AWS API Call via CloudTrail"],
                            detail={
                                "eventSource": ["kinesis.amazonaws.com"],
                                "eventName": ["CreateStream"],
                                "errorCode": [{"exists": False}]
                            }
                        )
                    )
        _kinesisRule.add_target(_targets.SfnStateMachine(kinesis_state_machine))

        # ==================================================================
        # PART 6: Redshift provisioned-cluster tagging via Step Functions
        # A cluster spends several minutes in "creating" before it is
        # "available", and Redshift emits no native readiness event, so the
        # workflow is triggered by the CloudTrail CreateCluster event and polls
        # DescribeClusters. Unlike every other polling workflow here, the ARN
        # cannot be read off the describe response -- DescribeClusters returns
        # no ARN field -- so the prepare Lambda builds it from the event's
        # account/region and the tag step uses that value.
        # ==================================================================
        redshift_lambda_role = _iam.Role(self, "redshift_tagging_lambda_role",
            role_name=f"redshift-tagging-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))
        redshift_lambda_role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        redshift_lambda_role.add_to_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            resources=["*"],
            actions=["redshift:DescribeClusters"]
        ))

        redshift_function = _lambda.Function(self, "redshift_tagging_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(60),
                                    handler="redshift_tagging.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="redshift-tagging-function",
                                    role=redshift_lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        redshift_sfn_log_group = _logs.LogGroup(self, "redshift_tagging_logs",
            log_group_name="/aws/vendedlogs/states/redshift-tagging",
            retention=_logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY)

        redshift_prepare_task = _tasks.LambdaInvoke(self, "RedshiftPrepareTags",
            lambda_function=redshift_function,
            payload_response_only=True)

        redshift_wait_task = _sfn.Wait(self, "RedshiftWaitForReady",
            time=_sfn.WaitTime.seconds_path("$.waitSeconds"))

        redshift_check_task = _tasks.LambdaInvoke(self, "RedshiftCheckReady",
            lambda_function=redshift_function,
            payload=_sfn.TaskInput.from_object({
                "action": "check",
                "clusterIdentifier": _sfn.JsonPath.string_at("$.clusterIdentifier"),
            }),
            result_path="$.check",
            payload_response_only=True)
        redshift_check_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(15),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        # DescribeClusters returns no ARN, so the tag call uses the ARN that
        # prepare built -- not a field from the check response.
        redshift_add_tags_task = _tasks.CallAwsService(self, "RedshiftAddTags",
            service="redshift",
            action="createTags",
            iam_action="redshift:CreateTags",
            iam_resources=["*"],
            parameters={
                "ResourceName": _sfn.JsonPath.string_at("$.resourceArn"),
                "Tags": _sfn.JsonPath.list_at("$.tagList")
            },
            result_path=_sfn.JsonPath.DISCARD)
        redshift_add_tags_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(30),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        redshift_succeed_state = _sfn.Succeed(self, "RedshiftTaggingComplete")
        redshift_fail_state = _sfn.Fail(self, "RedshiftTaggingFailed",
            cause="Failed to tag the Redshift cluster",
            error="RedshiftTaggingError")

        redshift_is_ready_choice = _sfn.Choice(self, "RedshiftIsClusterReady")
        redshift_is_ready_choice.when(
            _sfn.Condition.string_equals("$.check.status", "available"),
            redshift_add_tags_task.next(redshift_succeed_state))
        redshift_is_ready_choice.otherwise(
            redshift_wait_task.next(redshift_check_task))

        redshift_add_tags_task.add_catch(redshift_fail_state, errors=["States.ALL"])

        redshift_definition = redshift_prepare_task.next(redshift_check_task).next(
            redshift_is_ready_choice)

        redshift_state_machine = _sfn.StateMachine(self, "RedshiftTaggingStateMachine",
            state_machine_name="redshift-tagging-state-machine",
            definition_body=_sfn.DefinitionBody.from_chainable(redshift_definition),
            timeout=Duration.hours(2),
            logs=_sfn.LogOptions(
                destination=redshift_sfn_log_group,
                level=_sfn.LogLevel.ALL))

        # --- 6.4 EventBridge rule: Redshift cluster creation (CloudTrail) ---
        _redshiftRule = _events.Rule(self, "redshift-tagging-rule",
                        rule_name="redshift-tagging-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.redshift"],
                            detail_type=["AWS API Call via CloudTrail"],
                            detail={
                                "eventSource": ["redshift.amazonaws.com"],
                                "eventName": ["CreateCluster"],
                                "errorCode": [{"exists": False}]
                            }
                        )
                    )
        _redshiftRule.add_target(_targets.SfnStateMachine(redshift_state_machine))

        # ==================================================================
        # PART 7: Redshift Serverless workgroup tagging via Step Functions
        # Redshift Serverless splits into two resources with very different
        # timing. A NAMESPACE is AVAILABLE as soon as CreateNamespace returns, so
        # it is tagged immediately by the generic Lambda (PART 1). A WORKGROUP
        # spends minutes in CREATING and tagging during provisioning is not
        # documented as supported, so it polls GetWorkgroup here.
        # Both share the aws.redshift-serverless source but are split by
        # eventName across two rules, so they never both fire for one resource.
        # ==================================================================
        rss_lambda_role = _iam.Role(self, "redshift_serverless_tagging_lambda_role",
            role_name=f"redshift-serverless-tagging-role-{Aws.REGION}",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"))
        rss_lambda_role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"))
        rss_lambda_role.add_to_policy(_iam.PolicyStatement(
            effect=_iam.Effect.ALLOW,
            resources=["*"],
            actions=["redshift-serverless:GetWorkgroup"]
        ))

        rss_function = _lambda.Function(self, "redshift_serverless_tagging_function",
                                    runtime=_lambda.Runtime.PYTHON_3_13,
                                    memory_size=128,
                                    timeout=Duration.seconds(60),
                                    handler="redshift_serverless_tagging.main",
                                    code=_lambda.Code.from_asset("./lambda"),
                                    function_name="redshift-serverless-workgroup-tagging-function",
                                    role=rss_lambda_role,
                                    environment={
                                        "tags": tags.value_as_string,
                                        "identityRecording": identityRecording.value_as_string
                                    }
                        )

        rss_sfn_log_group = _logs.LogGroup(self, "redshift_serverless_tagging_logs",
            log_group_name="/aws/vendedlogs/states/redshift-serverless-workgroup-tagging",
            retention=_logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY)

        rss_prepare_task = _tasks.LambdaInvoke(self, "RSSPrepareTags",
            lambda_function=rss_function,
            payload_response_only=True)

        rss_wait_task = _sfn.Wait(self, "RSSWaitForReady",
            time=_sfn.WaitTime.seconds_path("$.waitSeconds"))

        rss_check_task = _tasks.LambdaInvoke(self, "RSSCheckReady",
            lambda_function=rss_function,
            payload=_sfn.TaskInput.from_object({
                "action": "check",
                "workgroupName": _sfn.JsonPath.string_at("$.workgroupName"),
            }),
            result_path="$.check",
            payload_response_only=True)
        rss_check_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(15),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        # The SDK integration service name has no dash (redshiftserverless) but
        # the IAM action does (redshift-serverless:), so iam_action is explicit.
        # Parameters are PascalCase here, unlike boto3's lower-case ones.
        rss_add_tags_task = _tasks.CallAwsService(self, "RSSAddTags",
            service="redshiftserverless",
            action="tagResource",
            iam_action="redshift-serverless:TagResource",
            iam_resources=["*"],
            parameters={
                "ResourceArn": _sfn.JsonPath.string_at("$.check.arn"),
                "Tags": _sfn.JsonPath.list_at("$.tagList")
            },
            result_path=_sfn.JsonPath.DISCARD)
        rss_add_tags_task.add_retry(
            errors=["States.ALL"],
            interval=Duration.seconds(30),
            max_attempts=5,
            backoff_rate=2.0,
            jitter_strategy=_sfn.JitterType.FULL)

        rss_succeed_state = _sfn.Succeed(self, "RSSTaggingComplete")
        rss_fail_state = _sfn.Fail(self, "RSSTaggingFailed",
            cause="Failed to tag the Redshift Serverless workgroup",
            error="RedshiftServerlessTaggingError")

        rss_is_ready_choice = _sfn.Choice(self, "RSSIsWorkgroupReady")
        rss_is_ready_choice.when(
            _sfn.Condition.string_equals("$.check.status", "AVAILABLE"),
            rss_add_tags_task.next(rss_succeed_state))
        rss_is_ready_choice.otherwise(
            rss_wait_task.next(rss_check_task))

        rss_add_tags_task.add_catch(rss_fail_state, errors=["States.ALL"])

        rss_definition = rss_prepare_task.next(rss_check_task).next(
            rss_is_ready_choice)

        rss_state_machine = _sfn.StateMachine(self, "RedshiftServerlessTaggingStateMachine",
            state_machine_name="redshift-serverless-workgroup-tagging-state-machine",
            definition_body=_sfn.DefinitionBody.from_chainable(rss_definition),
            timeout=Duration.hours(2),
            logs=_sfn.LogOptions(
                destination=rss_sfn_log_group,
                level=_sfn.LogLevel.ALL))

        # --- 7.4 EventBridge rule: workgroup creation only (CloudTrail) ---
        _rssRule = _events.Rule(self, "redshift-serverless-workgroup-tagging-rule",
                        rule_name="redshift-serverless-workgroup-tagging-rule",
                        event_pattern=_events.EventPattern(
                            source=["aws.redshift-serverless"],
                            detail_type=["AWS API Call via CloudTrail"],
                            detail={
                                "eventSource": ["redshift-serverless.amazonaws.com"],
                                "eventName": ["CreateWorkgroup"],
                                "errorCode": [{"exists": False}]
                            }
                        )
                    )
        _rssRule.add_target(_targets.SfnStateMachine(rss_state_machine))

        # ==================================================================
        # cdk_nag suppressions
        # AwsSolutionsChecks runs on synth (see app.py). The findings below are
        # inherent to a tagging-automation sample and are documented in
        # THREAT_MODEL.md (T1: tag-only + read-only wildcard scoping; T8:
        # unused/mutating grants already removed). Suppressions are applied to
        # the construct objects directly (not hard-coded paths) so they resolve
        # regardless of the stack id, and scoped via appliesTo.
        # ==================================================================

        # IAM4: every execution role attaches the AWS-managed
        # AWSLambdaBasicExecutionRole purely for CloudWatch Logs write access
        # (CreateLogGroup/Stream/PutLogEvents). This is the standard Lambda
        # logging policy; a customer-managed equivalent would add no security
        # value for a sample.
        _basic_exec = ("Policy::arn:<AWS::Partition>:iam::aws:policy/"
                       "service-role/AWSLambdaBasicExecutionRole")
        _iam4 = [{"id": "AwsSolutions-IAM4",
                  "reason": "Standard Lambda logging policy (CloudWatch Logs "
                            "write only); no resource-scope risk for this sample.",
                  "appliesTo": [_basic_exec]}]
        for _role in (lambda_role, rds_lambda_role, prepare_lambda_role, ec_lambda_role,
                      kinesis_lambda_role, redshift_lambda_role,
                      rss_lambda_role):
            NagSuppressions.add_resource_suppressions(_role, _iam4)

        # IAM5: tagging automation must act on resources whose ARNs are unknown
        # at deploy time (any future resource), so Resource::* is inherent. The
        # actions are tag-write + read-only Describe only -- no create/delete/
        # modify and no data-plane access (THREAT_MODEL.md T1/T8). Each path has
        # its own least-privilege role. apply_to_children reaches each role's
        # generated DefaultPolicy.
        _iam5_wildcard = [{"id": "AwsSolutions-IAM5",
                           "reason": "Tagging targets resources whose ARNs are "
                                     "unknown at deploy time; actions are tag-write "
                                     "+ read-only Describe only (THREAT_MODEL.md "
                                     "T1/T8).",
                           "appliesTo": ["Resource::*"]}]
        for _role in (lambda_role, rds_lambda_role, ec_lambda_role, kinesis_lambda_role,
                      redshift_lambda_role, rss_lambda_role):
            NagSuppressions.add_resource_suppressions(
                _role, _iam5_wildcard, apply_to_children=True)

        # IAM5 on the Step Functions roles: the CDK-generated role grants the
        # state machine permission to invoke its prepare/check Lambda (Arn:*
        # covers the function's versions/aliases) and to call the resource-level
        # describe/tag actions (Resource::*) for the same reason as above.
        for _sm, _fn in ((state_machine, prepare_function),
                         (ec_state_machine, ec_function),
                         (kinesis_state_machine, kinesis_function),
                         (redshift_state_machine, redshift_function),
                         (rss_state_machine, rss_function)):
            NagSuppressions.add_resource_suppressions(
                _sm,
                [{"id": "AwsSolutions-IAM5",
                  "reason": "State machine invokes its own prepare/check Lambda "
                            "(Arn:*) and calls Describe*/AddTags on a deploy-time-"
                            "unknown resource ARN (THREAT_MODEL.md T1).",
                  "appliesTo": [
                      {"regex": "/^Resource::<.*\\.Arn>:\\*$/"},
                      "Resource::*"]}],
                apply_to_children=True)

        # SF2: X-Ray tracing is an operational observability nicety, not a
        # security control. Out of scope for a tagging-automation sample;
        # consumers can enable it in production.
        _sf2 = [{"id": "AwsSolutions-SF2",
                 "reason": "X-Ray tracing is operational observability, not a "
                           "security control; out of scope for this sample."}]
        for _sm in (state_machine, ec_state_machine, kinesis_state_machine,
                    redshift_state_machine, rss_state_machine):
            NagSuppressions.add_resource_suppressions(_sm, _sf2)
