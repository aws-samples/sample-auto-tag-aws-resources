# Auto Tag Resource (Step Functions edition)

> This is sample code, for non-production usage. You should work with your
> security and legal teams to meet your organizational security, regulatory and
> compliance requirements before deployment.

This is an automated resource-tagging solution that tags newly created AWS
resources. It tags resources that are taggable immediately (synchronously, with
a Lambda), and handles resources that take time to provision out of band so
that tagging happens only once the resource is ready:

- **OpenSearch** and **ElastiCache** are tagged through AWS Step Functions
  workflows that poll until the resource is available.
- **RDS / Aurora** are tagged from the RDS *service event* that AWS emits once
  the DB instance/cluster is created.

## Why some resources are handled differently

Several services are **not taggable immediately** after the CloudTrail
`Create*` API call, because the resource is still provisioning. Tagging then
fails (e.g. OpenSearch returns `ValidationException: A change/update is in
progress`), and during **batch creation** the provisioning queue backs up,
so any synchronous boto3 waiter inside the Lambda times out.

This solution waits for each slow resource to become ready before tagging it,
using the most reliable readiness signal available per service:

| Service | Readiness signal | Mechanism |
|---|---|---|
| OpenSearch | `DescribeDomain.Processing == false` | Step Functions polling |
| ElastiCache | `Describe*.Status == available` | Step Functions polling |
| RDS / Aurora | `RDS-EVENT-0170` / `RDS-EVENT-0005` service event | EventBridge → Lambda |

> **Why ElastiCache polls instead of using events:** ElastiCache *does* emit
> `...ProvisioningComplete` / `CreateReplicationGroupComplete` lifecycle
> events, but only via Amazon SNS and only when the resource was created with a
> `NotificationTopicArn`. Because the creation path cannot be controlled for
> every resource, relying on those notifications would silently miss tags, so
> we poll instead.
>
> **Why RDS uses events instead of polling:** RDS/Aurora publish creation
> events natively to EventBridge (no SNS topic required), so no polling is
> needed.

## Architecture

```
                          CloudTrail (AWS API Call)                         RDS service event
                                   |                                       (aws.rds, EventBridge)
       +---------------+-----------+--------------------+                           |
       |               |                                |                  rds-tagging-on-ready
  generic rule    opensearch rule                elasticache rule          Lambda: tag SourceArn
  (ec2, s3, ...)  (CreateDomain / ...)           (CreateCacheCluster / ...)    (RDS-EVENT-0170
       |               |                                |                       RDS-EVENT-0005)
  resource-tagging  SFN state machine              SFN state machine
  Lambda            PrepareTags                    ECPrepareTags
  (tag now)              |                              |
                    DescribeDomain <--+            ECCheckReady <--+
                         |            |                 |          |
                    IsDomainReady?  Wait 60s       ECIsReady?    Wait 60-120s
                      |     |         |               |    |        |
                    (yes) (no)-------+             (yes) (no)------+
                      |                              |
                    AddTags -> Complete           ECAddTags -> Complete
```

- **Generic services** (EC2/EBS, S3, Lambda, DynamoDB, ELB, EFS, SQS, SNS, KMS,
  GameLift, CloudWatch Logs) are tagged immediately by the existing Lambda.
  The `aws.es`, `aws.rds`, and `aws.elasticache` sources have been **removed**
  from this rule.
- **OpenSearch** is routed to a Step Functions state machine that polls
  `DescribeDomain` until `Processing == false`, then calls `AddTags`.
- **ElastiCache** (cache cluster, replication group, serverless cache) is
  routed to a Step Functions state machine that polls `Describe*` until
  `Status == available`, then calls `AddTagsToResource`. The first poll waits a
  randomized 60–120s to spread `Describe*` calls across a batch.
- **RDS / Aurora** (and DocumentDB, which shares the `aws.rds` event stream) are
  tagged by a Lambda triggered by the RDS service event once the resource is
  created — `RDS-EVENT-0170` (DB cluster created) and `RDS-EVENT-0005` (DB
  instance created) — using `detail.SourceArn` directly.

> **Note on `identityRecording` for RDS/Aurora:** the RDS service event carries
> no `userIdentity`, so the requester identity cannot be recorded for RDS/Aurora
> resources. ElastiCache still supports `identityRecording` because its workflow
> is triggered by the CloudTrail Create event, which does carry `userIdentity`.

## OpenSearch API rename handling

When the service was renamed in 2021, the configuration API operation
`CreateElasticsearchDomain` was replaced by the engine-agnostic `CreateDomain`,
but **both API versions are still supported**. They share the same
`source` (`aws.es`) and `eventSource` (`es.amazonaws.com`) and differ only in
`eventName`. The OpenSearch EventBridge rule matches **both** event names so
domains created via either the new or legacy API are tagged.

> Note: the IAM action namespace for OpenSearch is still `es:` (e.g.
> `es:DescribeDomain`, `es:AddTags`), not `opensearch:`.

## Prerequisite

1. A machine to deploy CDK code, with AWS credentials configured.
2. Python 3.8+ and Node.js installed.

## To deploy

1. Ensure CDK is installed
```
$ npm install -g aws-cdk
```

2. Create a Python virtual environment
```
$ python3 -m venv .venv
```

3. Activate the virtual environment

_On macOS or Linux_
```
$ source .venv/bin/activate
```

_On Windows_
```
% .venv\Scripts\activate.bat
```

4. Install the required dependencies
```
$ pip install -r requirements.txt
```

5. Bootstrap the CDK environment
```
$ cdk bootstrap
```

6. Synthesize (`cdk synth`) or deploy (`cdk deploy`). Replace the tags with your own.
```
$ cdk deploy --require-approval never --parameters tags='{"TagName1": "TagValue1","TagName2": "TagValue2"}'
```

(Optional) Enable Identity Recording to also record the requester identity
(`userId`, and `roleId` for assumed roles) as tags:
```
$ cdk deploy --require-approval never --parameters tags='{"TagName1": "TagValue1","TagName2": "TagValue2"}' --parameters identityRecording='true'
```

## Run tests

```
$ pip install -r requirements-dev.txt
$ python -m pytest
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
