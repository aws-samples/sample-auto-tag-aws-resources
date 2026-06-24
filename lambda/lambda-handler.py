# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import boto3
import os
import json

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

# NOTE: ElastiCache (aws.elasticache) is intentionally NOT handled here.
# A cache cluster / replication group / serverless cache can take a long time
# to provision, especially during batch creation, and rejects tagging until it
# reaches the "available" state. The synchronous boto3 waiter previously used
# here would time out the Lambda under load. ElastiCache tagging is now handled
# asynchronously by a dedicated Step Functions workflow that polls Describe*
# until Status == available, then calls AddTagsToResource. See the CDK stack
# (PART 4) and lambda/elasticache_tagging.py.

def get_identity(event):
    print("getting user Identity...")
    _userId = event['detail']['userIdentity']['arn'].split('/')[-1]
    
    if event['detail']['userIdentity']['type'] == 'AssumedRole':
        _roleId = event['detail']['userIdentity']['arn'].split('/')[-2]
        return _userId, _roleId
    return _userId

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
}


def main(event, context):
    print(f"input event is: {event}")
    print("new source is ", event['source'])
    _source = event['source']
    _method = _source.replace('.', "_")

    handler = _SOURCE_HANDLERS.get(_source)
    if handler is None:
        raise ValueError(f"Unsupported event source: {_source}")
    resARNs = handler(event)
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

    # GameLift
    if _method == 'aws_gamelift':
        client = boto3.client('gamelift')
        tags = [{'Key': key, 'Value': value} for key, value in _res_tags.items()]
        for arn in resARNs:
            response = client.tag_resource(
                ResourceARN=arn,
                Tags=tags
            )
    if _method == 'aws_logs':
        client = boto3.client('logs')
        # Convert list of key-value pairs to dictionary for CloudWatch Logs
        tags = {key: value for key, value in _res_tags.items()}
        if event['detail']['eventName'] == 'CreateLogGroup':
            print("tagging for new cloudwatch logs...")
            _logGroupName = event['detail']['requestParameters']['logGroupName']
            response = client.tag_log_group(
                logGroupName=_logGroupName,
                tags=tags
            )
    # NOTE: RDS/DocumentDB and ElastiCache (incl. Serverless) are no longer
    # tagged from this Lambda. They are handled asynchronously once the
    # resource is available -- RDS/Aurora via the RDS service-event Lambda
    # (PART 3) and ElastiCache via a Step Functions workflow (PART 4).

    else:
        boto3.client('resourcegroupstaggingapi').tag_resources(
            ResourceARNList=resARNs,
            Tags=_res_tags
        )

    return {
        'statusCode': 200,
        'body': json.dumps('Finished tagging with ' + event['source'])
    }
