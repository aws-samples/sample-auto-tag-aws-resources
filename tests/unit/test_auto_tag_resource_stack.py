import aws_cdk as core
import aws_cdk.assertions as assertions
from cdk_nag import AwsSolutionsChecks

from auto_tag_resource.auto_tag_resource_stack import AutoTagResourceStack


def _build_template():
    app = core.App()
    stack = AutoTagResourceStack(app, "auto-tag-resource")
    return assertions.Template.from_stack(stack)


def test_state_machines_created():
    """Five Step Functions state machines: OpenSearch, ElastiCache, Kinesis,
    Redshift, Redshift Serverless workgroup."""
    template = _build_template()
    template.resource_count_is("AWS::StepFunctions::StateMachine", 5)


def test_lambdas_created():
    """Seven Lambda functions:
    - the generic tagger
    - the OpenSearch prepare step
    - the RDS/Aurora service-event tagger
    - the ElastiCache prepare/check step
    - the Kinesis prepare/check step
    - the Redshift prepare/check step
    - the Redshift Serverless workgroup prepare/check step
    """
    template = _build_template()
    template.resource_count_is("AWS::Lambda::Function", 7)


def test_opensearch_rule_matches_new_and_legacy_event_names():
    """The OpenSearch EventBridge rule must match both the new (CreateDomain)
    and legacy (CreateElasticsearchDomain) API event names."""
    template = _build_template()
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.es"],
            "detail": {
                "eventName": ["CreateDomain", "CreateElasticsearchDomain"]
            }
        }
    })


def test_generic_rule_no_longer_targets_opensearch_rds_or_elasticache():
    """The generic tagging rule must not reference es, rds, or elasticache
    sources anymore, since those are handled out of band (SFN / service event)."""
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    for rule in rules.values():
        pattern = rule["Properties"].get("EventPattern", {})
        sources = pattern.get("source", [])
        event_names = pattern.get("detail", {}).get("eventName", [])
        # The rule that lists many service sources is the generic one.
        if "aws.ec2" in sources:
            assert "aws.es" not in sources
            assert "aws.rds" not in sources
            assert "aws.elasticache" not in sources
            assert "CreateDomain" not in event_names
            assert "CreateDBInstance" not in event_names
            assert "CreateReplicationGroup" not in event_names
            assert "CreateCacheCluster" not in event_names
            assert "CreateServerlessCache" not in event_names


def test_generic_role_is_least_privilege():
    """The generic tagging role must not carry grants no handler uses
    (threat model T2/T8). In particular it must not be able to *remove* tags
    or describe network/CloudFormation resources it never touches."""
    template = _build_template()
    policies = template.find_resources("AWS::IAM::Policy")
    # Gather every action string across all inline policies.
    actions = set()
    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = stmt.get("Action", [])
            actions.update(action if isinstance(action, list) else [action])

    forbidden = {
        "tag:UntagResources",            # mutating: could strip tags
        "resource-groups:*",             # T2 wildcard
        "cloudformation:DescribeStacks",
        "cloudformation:ListStackResources",
        "ec2:DescribeNatGateways",
        "ec2:DescribeInternetGateways",
    }
    leaked = forbidden & actions
    assert not leaked, f"least-privilege regression: unused/mutating grants present: {sorted(leaked)}"

    # Sanity: the one tag-write the generic handler actually calls is still granted.
    assert "tag:TagResources" in actions


def test_rds_rule_matches_created_service_events():
    """RDS/Aurora are tagged from the RDS service event once available:
    RDS-EVENT-0170 (DB cluster created) and RDS-EVENT-0005 (DB instance created)."""
    template = _build_template()
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.rds"],
            "detail-type": ["RDS DB Cluster Event", "RDS DB Instance Event"],
            "detail": {
                "EventID": ["RDS-EVENT-0170", "RDS-EVENT-0005"]
            }
        }
    })


def test_elasticache_rule_matches_create_events():
    """ElastiCache is tagged via a Step Functions workflow triggered by the
    CloudTrail Create* events for cluster, replication group, and serverless."""
    template = _build_template()
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.elasticache"],
            "detail": {
                "eventName": ["CreateCacheCluster", "CreateReplicationGroup", "CreateServerlessCache"]
            }
        }
    })


def test_generic_rule_covers_msk_and_eks():
    """MSK and EKS accept tags on a still-provisioning cluster ARN, so they ride
    the generic immediate-tagging rule."""
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    generic = [r for r in rules.values()
               if "aws.ec2" in r["Properties"].get("EventPattern", {}).get("source", [])]
    assert len(generic) == 1
    pattern = generic[0]["Properties"]["EventPattern"]
    assert "aws.kafka" in pattern["source"]
    assert "aws.eks" in pattern["source"]
    assert "kafka.amazonaws.com" in pattern["detail"]["eventSource"]
    assert "eks.amazonaws.com" in pattern["detail"]["eventSource"]
    assert "CreateCluster" in pattern["detail"]["eventName"]
    assert "CreateClusterV2" in pattern["detail"]["eventName"]


def test_generic_role_grants_msk_and_eks_tag_actions():
    template = _build_template()
    policies = template.find_resources("AWS::IAM::Policy")
    actions = set()
    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = stmt.get("Action", [])
            actions.update(action if isinstance(action, list) else [action])
    assert "kafka:TagResource" in actions
    assert "eks:TagResource" in actions


def test_generic_rule_covers_ecs():
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    generic = [r for r in rules.values()
               if "aws.ec2" in r["Properties"].get("EventPattern", {}).get("source", [])][0]
    pattern = generic["Properties"]["EventPattern"]
    assert "aws.ecs" in pattern["source"]
    assert "ecs.amazonaws.com" in pattern["detail"]["eventSource"]
    assert "CreateService" in pattern["detail"]["eventName"]


def test_generic_rule_covers_msf():
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    generic = [r for r in rules.values()
               if "aws.ec2" in r["Properties"].get("EventPattern", {}).get("source", [])][0]
    pattern = generic["Properties"]["EventPattern"]
    assert "aws.kinesisanalytics" in pattern["source"]
    assert "kinesisanalytics.amazonaws.com" in pattern["detail"]["eventSource"]
    assert "CreateApplication" in pattern["detail"]["eventName"]


def test_generic_rule_covers_redshift_serverless_namespace_only():
    """aws.redshift-serverless appears on two rules: the generic one takes only
    CreateNamespace, the workgroup state machine takes only CreateWorkgroup."""
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    generic = [r for r in rules.values()
               if "aws.ec2" in r["Properties"].get("EventPattern", {}).get("source", [])][0]
    pattern = generic["Properties"]["EventPattern"]
    assert "aws.redshift-serverless" in pattern["source"]
    assert "CreateNamespace" in pattern["detail"]["eventName"]
    assert "CreateWorkgroup" not in pattern["detail"]["eventName"]


def test_kinesis_rule_starts_the_state_machine():
    """A Kinesis stream is CREATING right after CreateStream and only accepts
    tags once ACTIVE, so it is routed to a polling state machine."""
    template = _build_template()
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.kinesis"],
            "detail": {
                "eventSource": ["kinesis.amazonaws.com"],
                "eventName": ["CreateStream"]
            }
        }
    })


def test_no_unsuppressed_cdk_nag_findings():
    """cdk_nag gate.

    `python app.py` alone is NOT a gate: App.synth() does not fail on error
    annotations -- only the `cdk synth` CLI reports them. So the AwsSolutionsChecks
    aspect is applied here and the annotations are asserted directly, which keeps
    the check in the test suite where it runs on every change.
    """
    app = core.App()
    stack = AutoTagResourceStack(app, "auto-tag-resource")
    core.Aspects.of(stack).add(AwsSolutionsChecks(verbose=True))

    matcher = assertions.Match.string_like_regexp("AwsSolutions-.*")
    errors = assertions.Annotations.from_stack(stack).find_error("*", matcher)
    warnings = assertions.Annotations.from_stack(stack).find_warning("*", matcher)

    assert errors == [], (
        "unsuppressed cdk_nag errors: "
        + str([(e.id, e.entry.data) for e in errors]))
    assert warnings == [], (
        "unsuppressed cdk_nag warnings: "
        + str([(w.id, w.entry.data) for w in warnings]))


def test_redshift_rule_starts_the_state_machine():
    """A provisioned Redshift cluster takes minutes to become available, so it
    is routed to a polling state machine rather than the generic Lambda."""
    template = _build_template()
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.redshift"],
            "detail": {
                "eventSource": ["redshift.amazonaws.com"],
                "eventName": ["CreateCluster"]
            }
        }
    })


def test_generic_rule_does_not_target_redshift_or_kinesis():
    """Both are handled by their own state machines; a stray source here would
    make the generic Lambda try to tag a still-provisioning resource."""
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    generic = [r for r in rules.values()
               if "aws.ec2" in r["Properties"].get("EventPattern", {}).get("source", [])][0]
    sources = generic["Properties"]["EventPattern"]["source"]
    assert "aws.redshift" not in sources
    assert "aws.kinesis" not in sources


def test_redshift_serverless_workgroup_rule_starts_the_state_machine():
    """aws.redshift-serverless is split across two rules by eventName: the
    generic rule takes CreateNamespace, this one takes CreateWorkgroup."""
    template = _build_template()
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.redshift-serverless"],
            "detail": {
                "eventSource": ["redshift-serverless.amazonaws.com"],
                "eventName": ["CreateWorkgroup"]
            }
        }
    })


def test_cloudtrail_rules_ignore_failed_api_calls():
    """CloudTrail also records rejected Create* calls, which created nothing to
    tag. Every CloudTrail-driven rule must require errorCode to be absent,
    otherwise a failed call makes the generic Lambda tag a non-existent ARN and
    makes the polling workflows poll a resource that never appears. The RDS rule
    is exempt: it listens to an RDS service event, not a CloudTrail call."""
    template = _build_template()
    rules = template.find_resources("AWS::Events::Rule")
    cloudtrail_rules = 0
    for rule in rules.values():
        pattern = rule["Properties"].get("EventPattern", {})
        detail = pattern.get("detail", {})
        if "eventSource" not in detail:
            # The RDS service-event rule filters on EventID, not eventSource.
            assert "EventID" in detail
            continue
        cloudtrail_rules += 1
        assert detail.get("errorCode") == [{"exists": False}], \
            f"rule with sources {pattern.get('source')} accepts failed calls"
    # generic + glue/athena + opensearch + elasticache + kinesis + redshift
    # + rs-serverless
    assert cloudtrail_rules == 7


def _generic_rule(template):
    rules = template.find_resources("AWS::Events::Rule")
    return [r for r in rules.values()
            if "aws.ec2" in r["Properties"].get("EventPattern", {}).get("source", [])][0]


def _glue_athena_rule(template):
    rules = template.find_resources("AWS::Events::Rule")
    return [r for r in rules.values()
            if "aws.glue" in r["Properties"].get("EventPattern", {}).get("source", [])][0]


def test_glue_and_athena_have_their_own_rule():
    """Glue and Athena tag immediately (same Lambda as the generic rule) but need
    a separate rule: the generic rule's flat eventName list carries DynamoDB's
    CreateTable, and Glue emits CreateTable per crawler-discovered table for a
    resource that is not taggable at all."""
    template = _build_template()
    pattern = _glue_athena_rule(template)["Properties"]["EventPattern"]
    assert sorted(pattern["source"]) == ["aws.athena", "aws.glue"]
    assert sorted(pattern["detail"]["eventSource"]) == [
        "athena.amazonaws.com", "glue.amazonaws.com"]
    for name in ("CreateDatabase", "CreateCrawler", "CreateJob", "CreateTrigger",
                 "CreateWorkflow", "CreateSession",
                 "CreateWorkGroup", "CreateDataCatalog", "CreateCapacityReservation"):
        assert name in pattern["detail"]["eventName"]


def test_glue_athena_rule_excludes_untaggable_glue_tables():
    """A Glue table cannot be tagged (glue:TagResource returns
    InvalidInputException for a table ARN), so CreateTable must not be on this
    rule -- otherwise every crawler-discovered table invokes the Lambda to no-op."""
    template = _build_template()
    pattern = _glue_athena_rule(template)["Properties"]["EventPattern"]
    assert "CreateTable" not in pattern["detail"]["eventName"]


def test_glue_athena_rule_excludes_connections():
    """Connections are out of scope because their tag path needs
    glue:GetConnection, which can return a plaintext JDBC password."""
    template = _build_template()
    pattern = _glue_athena_rule(template)["Properties"]["EventPattern"]
    assert "CreateConnection" not in pattern["detail"]["eventName"]


def test_generic_role_can_read_glue_databases_but_not_connections():
    """Tagging a Data Catalog database ARN makes Glue run an internal existence
    check that fails with AccessDenied unless glue:GetDatabase is granted
    (confirmed end-to-end in us-east-1). glue:GetConnection must NOT be granted:
    it can return a connection's plaintext PASSWORD, which would break the
    "tag-write plus non-secret reads only" scope in THREAT_MODEL.md T1."""
    template = _build_template()
    policies = template.find_resources("AWS::IAM::Policy")
    actions = set()
    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = stmt.get("Action", [])
            actions.update(action if isinstance(action, list) else [action])
    assert "glue:GetDatabase" in actions
    assert "glue:GetConnection" not in actions
    # No broader Data Catalog reads leaked in alongside it.
    assert "glue:GetTable" not in actions
    assert "glue:GetTables" not in actions


def test_generic_rule_does_not_target_glue_or_athena():
    """Both ride their own rule; leaving them on the generic rule would re-admit
    the Glue CreateTable overlap with DynamoDB."""
    template = _build_template()
    sources = _generic_rule(template)["Properties"]["EventPattern"]["source"]
    assert "aws.glue" not in sources
    assert "aws.athena" not in sources


def test_athena_workgroup_event_name_does_not_collide_with_redshift_serverless():
    """Athena emits CreateWork*G*roup and Redshift Serverless CreateWorkgroup.
    EventBridge matches eventName exactly, so each rule must carry only its own
    spelling."""
    template = _build_template()
    glue_athena = _glue_athena_rule(template)["Properties"]["EventPattern"]
    assert "CreateWorkGroup" in glue_athena["detail"]["eventName"]
    assert "CreateWorkgroup" not in glue_athena["detail"]["eventName"]

    rules = template.find_resources("AWS::Events::Rule")
    rss = [r for r in rules.values()
           if r["Properties"].get("EventPattern", {}).get("source") == ["aws.redshift-serverless"]][0]
    assert rss["Properties"]["EventPattern"]["detail"]["eventName"] == ["CreateWorkgroup"]


def test_generic_role_grants_glue_and_athena_tag_actions():
    template = _build_template()
    policies = template.find_resources("AWS::IAM::Policy")
    actions = set()
    for policy in policies.values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = stmt.get("Action", [])
            actions.update(action if isinstance(action, list) else [action])
    assert "glue:TagResource" in actions
    assert "athena:TagResource" in actions
