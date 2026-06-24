import aws_cdk as core
import aws_cdk.assertions as assertions

from auto_tag_resource.auto_tag_resource_stack import AutoTagResourceStack


def _build_template():
    app = core.App()
    stack = AutoTagResourceStack(app, "auto-tag-resource")
    return assertions.Template.from_stack(stack)


def test_state_machines_created():
    """Two Step Functions state machines: OpenSearch and ElastiCache tagging."""
    template = _build_template()
    template.resource_count_is("AWS::StepFunctions::StateMachine", 2)


def test_lambdas_created():
    """Four Lambda functions:
    - the generic tagger
    - the OpenSearch prepare step
    - the RDS/Aurora service-event tagger
    - the ElastiCache prepare/check step
    """
    template = _build_template()
    template.resource_count_is("AWS::Lambda::Function", 4)


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
