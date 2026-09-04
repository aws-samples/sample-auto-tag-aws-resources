# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import boto3
import os
import json
import time

from botocore.exceptions import ClientError

def aws_ec2(event):
    arnList = []
    _account = event['account']
    _region = event['region']
    ec2ArnTemplate = 'arn:aws:ec2:@region@:@account@:instance/@instanceId@'
    volumeArnTemplate = 'arn:aws:ec2:@region@:@account@:volume/@volumeId@'
    ec2_resource = boto3.resource('ec2')
    if event['detail']['eventName'] == 'RunInstances':
        print("tagging for new EC2...")
        for item in event['detail']['responseElements']['instancesSet']['items']:
            _instanceId = item['instanceId']
            arnList.append(ec2ArnTemplate.replace('@region@', _region).replace('@account@', _account).replace('@instanceId@', _instanceId))

            _instance = ec2_resource.Instance(_instanceId)
            for volume in _instance.volumes.all():
                arnList.append(volumeArnTemplate.replace('@region@', _region).replace('@account@', _account).replace('@volumeId@', volume.id))

    elif event['detail']['eventName'] == 'CreateVolume':
        print("tagging for new EBS...")
        _volumeId = event['detail']['responseElements']['volumeId']
        arnList.append(volumeArnTemplate.replace('@region@', _region).replace('@account@', _account).replace('@volumeId@', _volumeId))
        
    elif event['detail']['eventName'] == 'CreateInternetGateway':
        print("tagging for new IGW...")
        
    elif event['detail']['eventName'] == 'CreateNatGateway':
        print("tagging for new Nat Gateway...")
        
    elif event['detail']['eventName'] == 'AllocateAddress':
        print("tagging for new EIP...")
        arnList.append(event['detail']['responseElements']['allocationId'])
        
    elif event['detail']['eventName'] == 'CreateVpcEndpoint':
        print("tagging for new VPC Endpoint...")
        
    elif event['detail']['eventName'] == 'CreateTransitGateway':
        print("tagging for new Transit Gateway...")

    return arnList
    
def aws_elasticloadbalancing(event):
    arnList = []
    if event['detail']['eventName'] == 'CreateLoadBalancer':
        print("tagging for new LoadBalancer...")
        lbs = event['detail']['responseElements']
        for lb in lbs['loadBalancers']:
            arnList.append(lb['loadBalancerArn'])
        return arnList

# NOTE: RDS / Aurora (aws.rds) is intentionally NOT handled here.
# A DB instance/cluster is not immediately taggable right after the
# CloudTrail CreateDBInstance / CreateDBCluster event (it is still being
# provisioned), and batch creation makes any synchronous waiter time out.
# RDS and Aurora are now tagged asynchronously from the RDS service event
# ("DB instance/cluster created", RDS-EVENT-0005 / RDS-EVENT-0170), which
# AWS emits once the resource is available. See the CDK stack (PART 3) and
# lambda/tag_rds_on_ready.py.

def aws_s3(event):
    arnList = []
    if event['detail']['eventName'] == 'CreateBucket':
        print("tagging for new S3...")
        _bkcuetName = event['detail']['requestParameters']['bucketName']
        arnList.append('arn:aws:s3:::' + _bkcuetName)
        return arnList
        
def aws_lambda(event):
    arnList = []
    _exist1 = event['detail']['responseElements']
    _exist2 = event['detail']['eventName'] == 'CreateFunction20150331'
    if  _exist1!= None and _exist2:
        function_name = event['detail']['responseElements']['functionName']
        print('Functin name is :', function_name)
        arnList.append(event['detail']['responseElements']['functionArn'])
        return arnList

def aws_dynamodb(event):
    arnList = []
    if event['detail']['eventName'] == 'CreateTable':
        table_name = event['detail']['responseElements']['tableDescription']['tableName']
        waiter = boto3.client('dynamodb').get_waiter('table_exists')
        waiter.wait(
            TableName=table_name,
            WaiterConfig={
                'Delay': 123,
                'MaxAttempts': 123
            }
        )
        arnList.append(event['detail']['responseElements']['tableDescription']['tableArn'])
        return arnList
        
def aws_kms(event):
    arnList = []
    if event['detail']['eventName'] == 'CreateKey':
        arnList.append(event['detail']['responseElements']['keyMetadata']['arn'])
        return arnList

def aws_sns(event):
    arnList = []
    _account = event['account']
    _region = event['region']
    snsArnTemplate = 'arn:aws:sns:@region@:@account@:@topicName@'
    if event['detail']['eventName'] == 'CreateTopic':
        print("tagging for new SNS...")
        _topicName = event['detail']['requestParameters']['name']
        arnList.append(snsArnTemplate.replace('@region@', _region).replace('@account@', _account).replace('@topicName@', _topicName))
        return arnList
        
def aws_sqs(event):
    arnList = []
    _account = event['account']
    _region = event['region']
    sqsArnTemplate = 'arn:aws:sqs:@region@:@account@:@queueName@'
    if event['detail']['eventName'] == 'CreateQueue':
        print("tagging for new SQS...")
        _queueName = event['detail']['requestParameters']['queueName']
        arnList.append(sqsArnTemplate.replace('@region@', _region).replace('@account@', _account).replace('@queueName@', _queueName))
        return arnList
        
def aws_elasticfilesystem(event):
    arnList = []
    _account = event['account']
    _region = event['region']
    efsArnTemplate = 'arn:aws:elasticfilesystem:@region@:@account@:file-system/@fileSystemId@'
    if event['detail']['eventName'] == 'CreateMountTarget':
        print("tagging for new efs...")
        _efsId = event['detail']['responseElements']['fileSystemId']
        arnList.append(efsArnTemplate.replace('@region@', _region).replace('@account@', _account).replace('@fileSystemId@', _efsId))
        return arnList
        
# NOTE: OpenSearch (aws.es) is intentionally NOT handled here.
# An OpenSearch domain rejects tag/config changes while it is still in the
# "Processing" state right after CreateDomain, which makes synchronous tagging
# from this Lambda fail with a ValidationException. OpenSearch tagging is now
# handled asynchronously by a dedicated Step Functions workflow that waits for
# the domain to become ready before calling AddTags. See the CDK stack
# (PART 2) and lambda/prepare_opensearch_tags.py.

def aws_gamelift(event):
    arnList = []
    if event['detail']['eventName'] == 'CreateFleet':
        print("tagging for new game lift...")
        arnList.append(event['detail']['responseElements']['fleetAttributes']['fleetArn'])
        return arnList

def aws_logs(event):
    arnList = []
    # dont need to get ARN for aws logs.
    return arnList

def aws_kafka(event):
    """Amazon MSK. A cluster stays CREATING for 15-30 minutes, but
    kafka:TagResource accepts the ARN immediately, so no polling is needed.
    CreateCluster (v1) and CreateClusterV2 both return responseElements.clusterArn.
    """
    arnList = []
    if event['detail']['eventName'] in ('CreateCluster', 'CreateClusterV2'):
        print("tagging for new MSK cluster...")
        response = event['detail']['responseElements'] or {}
        arn = response.get('clusterArn')
        if arn:
            arnList.append(arn)
    return arnList

def aws_eks(event):
    """Amazon EKS. Like MSK, eks:TagResource works on a CREATING cluster ARN."""
    arnList = []
    if event['detail']['eventName'] == 'CreateCluster':
        print("tagging for new EKS cluster...")
        response = event['detail']['responseElements'] or {}
        arn = (response.get('cluster') or {}).get('arn')
        if arn:
            arnList.append(arn)
    return arnList

def aws_ecs(event):
    """Amazon ECS clusters and services. Both are taggable as soon as they are
    created; only the service has a brief eventual-consistency window."""
    arnList = []
    response = event['detail']['responseElements'] or {}
    eventName = event['detail']['eventName']
    if eventName == 'CreateCluster':
        print("tagging for new ECS cluster...")
        arn = (response.get('cluster') or {}).get('clusterArn')
        if arn:
            arnList.append(arn)
    elif eventName == 'CreateService':
        print("tagging for new ECS service...")
        arn = (response.get('service') or {}).get('serviceArn')
        if arn:
            arnList.append(arn)
    return arnList

# NOTE: ElastiCache (aws.elasticache) is intentionally NOT handled here.
# A cache cluster / replication group / serverless cache can take a long time
# to provision, especially during batch creation, and rejects tagging until it
# reaches the "available" state. The synchronous boto3 waiter previously used
# here would time out the Lambda under load. ElastiCache tagging is now handled
# asynchronously by a dedicated Step Functions workflow that polls Describe*
# until Status == available, then calls AddTagsToResource. See the CDK stack
# (PART 4) and lambda/elasticache_tagging.py.
#
# Summary of what this Lambda does NOT tag, and where it happens instead:
#   - RDS / Aurora / DocumentDB -> RDS service-event Lambda   (PART 3)
#   - OpenSearch                -> Step Functions workflow    (PART 2)
#   - ElastiCache (incl. Serverless) -> Step Functions workflow (PART 4)

def get_identity(event):
    print("getting user Identity...")
    _userId = event['detail']['userIdentity']['arn'].split('/')[-1]
    
    if event['detail']['userIdentity']['type'] == 'AssumedRole':
        _roleId = event['detail']['userIdentity']['arn'].split('/')[-2]
        return _userId, _roleId
    return _userId

def aws_kinesisanalytics(event):
    """Amazon Managed Service for Apache Flink (formerly Kinesis Data
    Analytics). CreateApplication returns a READY application, so it is
    taggable immediately. The event source is still aws.kinesisanalytics even
    though the API/boto3 client is the v2 one."""
    arnList = []
    if event['detail']['eventName'] == 'CreateApplication':
        print("tagging for new Managed Service for Apache Flink application...")
        response = event['detail']['responseElements'] or {}
        detail = response.get('applicationDetail') or {}
        arn = detail.get('applicationARN') or response.get('applicationARN')
        if arn:
            arnList.append(arn)
    return arnList

# Glue resource types this handler tags, keyed by CloudTrail eventName. Each
# entry is (ARN resource type, name extractor). No Glue Create* API returns an
# ARN -- most return an empty body and the rest return just a name -- so the ARN
# is rebuilt from the event's account/region, like SNS/SQS above.
#
# Glue *tables* are deliberately absent: glue:TagResource rejects a
# table/<database>/<table> ARN with InvalidInputException (verified against
# us-east-1), as it does tableVersion, userDefinedFunction, and the root
# catalog ("Tag operations not supported on root catalog"). The Data Catalog
# only supports tags down to the database level.
#
# Glue *connections* are also deliberately absent, for a security reason rather
# than an API one: tagging a connection ARN makes Glue perform an internal
# existence check that requires glue:GetConnection, and GetConnection with
# HidePassword=false returns the connection's plaintext PASSWORD property.
# Granting that to a tagging role would let it read JDBC credentials, which
# contradicts the "tag-write plus non-secret reads only" scope in
# THREAT_MODEL.md T1. A connection is unbilled config, so it is not worth it.
#
# Taggable Glue types not covered here, should anyone need them: devEndpoint,
# mlTransform, registry, schema, blueprint, dataQualityRuleset.
_GLUE_RESOURCES = {
    "CreateDatabase":   ("database",
                         lambda req, res: (req.get('databaseInput') or {}).get('name')),
    "CreateCrawler":    ("crawler",
                         lambda req, res: req.get('name')),
    "CreateJob":        ("job",
                         lambda req, res: res.get('name') or req.get('name')),
    "CreateTrigger":    ("trigger",
                         lambda req, res: res.get('name') or req.get('name')),
    "CreateWorkflow":   ("workflow",
                         lambda req, res: res.get('name') or req.get('name')),
    "CreateSession":    ("session",
                         lambda req, res: (res.get('session') or {}).get('id') or req.get('id')),
}


def aws_glue(event):
    """AWS Glue. Every resource here is a Data Catalog / orchestration object
    that exists as soon as its Create* call returns, so it is taggable
    immediately -- no polling needed."""
    arnList = []
    entry = _GLUE_RESOURCES.get(event['detail']['eventName'])
    if entry is None:
        return arnList
    resourceType, extract = entry
    name = extract(event['detail'].get('requestParameters') or {},
                   event['detail'].get('responseElements') or {})
    if name:
        print("tagging for new Glue " + resourceType + "...")
        arnList.append('arn:aws:glue:{}:{}:{}/{}'.format(
            event['region'], event['account'], resourceType, name))
    return arnList


# Athena's three taggable resource types. All are created synchronously and
# named by requestParameters.name.
#
# Note the casing: Athena's event is CreateWork*G*roup, while Redshift
# Serverless emits CreateWorkgroup. EventBridge matches eventName exactly, so
# the two never collide -- do not "normalize" either spelling.
_ATHENA_RESOURCES = {
    "CreateWorkGroup": "workgroup",
    "CreateDataCatalog": "datacatalog",
    "CreateCapacityReservation": "capacity-reservation",
}


def aws_athena(event):
    """Amazon Athena workgroups, data catalogs, and capacity reservations."""
    arnList = []
    resourceType = _ATHENA_RESOURCES.get(event['detail']['eventName'])
    if resourceType is None:
        return arnList
    name = (event['detail'].get('requestParameters') or {}).get('name')
    if name:
        print("tagging for new Athena " + resourceType + "...")
        arnList.append('arn:aws:athena:{}:{}:{}/{}'.format(
            event['region'], event['account'], resourceType, name))
    return arnList


def aws_redshift_serverless(event):
    """Redshift Serverless *namespace* only. A namespace is AVAILABLE as soon as
    CreateNamespace returns (it has no CREATING state), so it is tagged here. A
    *workgroup* takes minutes to provision and has its own Step Functions
    workflow -- see the CDK stack, PART 7."""
    arnList = []
    if event['detail']['eventName'] == 'CreateNamespace':
        print("tagging for new Redshift Serverless namespace...")
        response = event['detail']['responseElements'] or {}
        arn = (response.get('namespace') or {}).get('namespaceArn')
        if arn:
            arnList.append(arn)
    return arnList

# Explicit dispatch table: maps an EventBridge `source` to its handler.
# Using an allowlisted dict instead of globals()[source] prevents an
# attacker-influenced source value from resolving to an arbitrary module
# attribute (defense-in-depth for T3; also clears semgrep dangerous-globals-use).
_SOURCE_HANDLERS = {
    "aws.ec2": aws_ec2,
    "aws.elasticloadbalancing": aws_elasticloadbalancing,
    "aws.s3": aws_s3,
    "aws.lambda": aws_lambda,
    "aws.dynamodb": aws_dynamodb,
    "aws.kms": aws_kms,
    "aws.sns": aws_sns,
    "aws.sqs": aws_sqs,
    "aws.elasticfilesystem": aws_elasticfilesystem,
    "aws.gamelift": aws_gamelift,
    "aws.logs": aws_logs,
    "aws.kafka": aws_kafka,
    "aws.eks": aws_eks,
    "aws.ecs": aws_ecs,
    "aws.kinesisanalytics": aws_kinesisanalytics,
    "aws.glue": aws_glue,
    "aws.athena": aws_athena,
    "aws.redshift-serverless": aws_redshift_serverless,
}


def _tag_with_retry(call, retry_error_codes, attempts=5, delay=3):
    """Run a tag call, retrying only the transient error codes a just-created
    resource can raise before it is visible to (or settled enough for) its
    tagging API. Any other error, or the final attempt, propagates."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code not in retry_error_codes or attempt == attempts:
                raise
            print(f"tagging attempt {attempt} failed with {code}; "
                  f"retrying in {delay}s")
            time.sleep(delay)


# A CreateService event can arrive before the service is visible to the tagging
# API. InvalidParameterException is deliberately NOT retried -- it signals a bad
# request, so retrying would only stall the invocation.
_ECS_RETRY_CODES = ("ResourceNotFoundException", "ClusterNotFoundException")

# A brand-new MSF application can still be settling when the event arrives.
_MSF_RETRY_CODES = ("ResourceInUseException", "ConcurrentModificationException")


# Services whose tag API is not reachable through the Resource Groups Tagging
# API (different parameter names, different tag structure, or tagging by name
# instead of by ARN) get a native tagger here. Any source not in this table
# falls back to resourcegroupstaggingapi.tag_resources.
# Signature: tagger(arn_list, res_tags, event) -> None


def _tag_gamelift(arn_list, res_tags, event):
    client = boto3.client('gamelift')
    tags = [{'Key': key, 'Value': value} for key, value in res_tags.items()]
    for arn in arn_list:
        client.tag_resource(ResourceARN=arn, Tags=tags)


def _tag_logs(arn_list, res_tags, event):
    # CloudWatch Logs is tagged by log group *name*, not by ARN, so arn_list is
    # unused here (aws_logs returns an empty list by design).
    if event['detail']['eventName'] == 'CreateLogGroup':
        print("tagging for new cloudwatch logs...")
        boto3.client('logs').tag_log_group(
            logGroupName=event['detail']['requestParameters']['logGroupName'],
            tags={key: value for key, value in res_tags.items()}
        )


def _tag_kafka(arn_list, res_tags, event):
    """MSK TagResource takes PascalCase params and a {key: value} tag map."""
    client = boto3.client('kafka')
    tags = {str(key): str(value) for key, value in res_tags.items()}
    for arn in arn_list:
        client.tag_resource(ResourceArn=arn, Tags=tags)


def _tag_eks(arn_list, res_tags, event):
    """EKS TagResource takes camelCase params and a {key: value} tag map."""
    client = boto3.client('eks')
    tags = {str(key): str(value) for key, value in res_tags.items()}
    for arn in arn_list:
        client.tag_resource(resourceArn=arn, tags=tags)


def _tag_ecs(arn_list, res_tags, event):
    """ECS TagResource takes a list of LOWER-case {key, value} objects."""
    client = boto3.client('ecs')
    tags = [{'key': str(key), 'value': str(value)}
            for key, value in res_tags.items()]
    for arn in arn_list:
        _tag_with_retry(
            lambda: client.tag_resource(resourceArn=arn, tags=tags),
            _ECS_RETRY_CODES)


def _tag_kinesisanalytics(arn_list, res_tags, event):
    """MSF TagResource takes a list of UPPER-case {Key, Value} objects, and its
    ARN parameter is ResourceARN (not ResourceArn)."""
    client = boto3.client('kinesisanalyticsv2')
    tags = [{'Key': str(key), 'Value': str(value)}
            for key, value in res_tags.items()]
    for arn in arn_list:
        _tag_with_retry(
            lambda: client.tag_resource(ResourceARN=arn, Tags=tags),
            _MSF_RETRY_CODES)


def _tag_glue(arn_list, res_tags, event):
    """Glue TagResource is the odd one out twice over: the tag parameter is named
    TagsToAdd (not Tags) and it takes a {key: value} map.

    Note for whoever extends _GLUE_RESOURCES: tagging a Data Catalog ARN makes
    Glue run an existence check under the hood that needs a matching read grant
    (a database needs glue:GetDatabase). The crawler/job/trigger/workflow/session
    ARNs need nothing beyond glue:TagResource."""
    client = boto3.client('glue')
    tags = {str(key): str(value) for key, value in res_tags.items()}
    for arn in arn_list:
        client.tag_resource(ResourceArn=arn, TagsToAdd=tags)


def _tag_athena(arn_list, res_tags, event):
    """Athena TagResource takes ResourceARN (all-caps ARN, like MSF) and a list
    of UPPER-case {Key, Value} objects."""
    client = boto3.client('athena')
    tags = [{'Key': str(key), 'Value': str(value)}
            for key, value in res_tags.items()]
    for arn in arn_list:
        client.tag_resource(ResourceARN=arn, Tags=tags)


def _tag_redshift_serverless(arn_list, res_tags, event):
    """Redshift Serverless TagResource takes camelCase params and a list of
    LOWER-case {key, value} objects. Note the Step Functions path (PART 7) uses
    the PascalCase SDK-integration spelling instead -- do not copy this shape
    into the state machine."""
    client = boto3.client('redshift-serverless')
    tags = [{'key': str(key), 'value': str(value)}
            for key, value in res_tags.items()]
    for arn in arn_list:
        client.tag_resource(resourceArn=arn, tags=tags)


_NATIVE_TAGGERS = {
    "aws.gamelift": _tag_gamelift,
    "aws.logs": _tag_logs,
    "aws.kafka": _tag_kafka,
    "aws.eks": _tag_eks,
    "aws.ecs": _tag_ecs,
    "aws.kinesisanalytics": _tag_kinesisanalytics,
    "aws.glue": _tag_glue,
    "aws.athena": _tag_athena,
    "aws.redshift-serverless": _tag_redshift_serverless,
}


def main(event, context):
    print(f"input event is: {event}")
    print("new source is ", event['source'])
    _source = event['source']

    handler = _SOURCE_HANDLERS.get(_source)
    if handler is None:
        raise ValueError(f"Unsupported event source: {_source}")
    # A handler returns None when the eventName is one it does not act on.
    resARNs = handler(event) or []
    print("resource arn is: ", resARNs)

    _res_tags =  json.loads(os.environ['tags'])
    _identity_recording = os.environ['identityRecording']

    if _identity_recording == 'true':
        if event['detail']['userIdentity']['type'] == 'AssumedRole':
            _userId, _roleId = get_identity(event)
            _res_tags['roleId'] = _roleId
        else:
            _userId = get_identity(event)
        
        _res_tags['userId'] = _userId
    
    print(_res_tags)

    # Services with their own tag API go through _NATIVE_TAGGERS; everything
    # else is tagged generically. This used to be an if/if/else chain whose
    # else bound only to the last if, so GameLift was tagged twice -- natively
    # and then again through the Resource Groups Tagging API.
    tagger = _NATIVE_TAGGERS.get(_source)
    if tagger is not None:
        tagger(resARNs, _res_tags, event)
    elif resARNs:
        boto3.client('resourcegroupstaggingapi').tag_resources(
            ResourceARNList=resARNs,
            Tags=_res_tags
        )
    else:
        print(f"no ARNs resolved for source {_source}; nothing to tag")

    return {
        'statusCode': 200,
        'body': json.dumps('Finished tagging with ' + event['source'])
    }
