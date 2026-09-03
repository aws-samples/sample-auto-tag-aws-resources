# Auto Tag Resource (Step Functions edition)

> This is sample code, for non-production usage. You should work with your
> security and legal teams to meet your organizational security, regulatory and
> compliance requirements before deployment.

This is an automated resource-tagging solution that tags newly created AWS
resources. It tags resources that are taggable immediately (synchronously, with
a Lambda), and handles resources that take time to provision out of band so
that tagging happens only once the resource is ready:

- **OpenSearch**, **ElastiCache**, **Kinesis Data Streams**, **Redshift**
  (provisioned), and **Redshift Serverless workgroups** are tagged through AWS
  Step Functions workflows that poll until the resource is available.
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
| Kinesis Data Streams | `DescribeStreamSummary.StreamStatus == ACTIVE` | Step Functions polling |
| Redshift (provisioned) | `DescribeClusters.ClusterStatus == available` | Step Functions polling |
| Redshift Serverless workgroup | `GetWorkgroup.status == AVAILABLE` | Step Functions polling |
| RDS / Aurora | `RDS-EVENT-0170` / `RDS-EVENT-0005` service event | EventBridge → Lambda |

Everything else is tagged immediately by the generic Lambda. Some of those are
still provisioning at that point, which is fine because their tag API accepts
the ARN anyway:

| Service | Note |
|---|---|
| EC2/EBS, S3, Lambda, DynamoDB, ELB, EFS, SQS, SNS, KMS, GameLift, CloudWatch Logs | taggable on creation |
| MSK cluster | `kafka:TagResource` accepts a CREATING cluster ARN |
| EKS cluster | `eks:TagResource` accepts a CREATING cluster ARN |
| ECS cluster / service | taggable on creation; the service has a short eventual-consistency window, retried in the Lambda |
| Managed Service for Apache Flink application | `CreateApplication` returns a READY application |
| Redshift Serverless **namespace** | AVAILABLE on creation (no CREATING state) — the **workgroup** polls instead |

> Six of these services cannot go through the Resource Groups Tagging API: their
> tag APIs disagree on parameter casing and tag structure (map vs upper-case
> `[{Key, Value}]` vs lower-case `[{key, value}]`), so each has a small native
> tagger in `_NATIVE_TAGGERS`.

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

| Trigger | Path |
|---|---|
| CloudTrail `Create*` for the immediate services | `resource-tagging-automation-rule` → generic Lambda → tag now |
| CloudTrail `CreateDomain` / `CreateElasticsearchDomain` | `opensearch-tagging-rule` → OpenSearch state machine |
| CloudTrail ElastiCache `Create*` | `elasticache-tagging-rule` → ElastiCache state machine |
| CloudTrail `CreateStream` | `kinesis-tagging-rule` → Kinesis state machine |
| CloudTrail Redshift `CreateCluster` | `redshift-tagging-rule` → Redshift state machine |
| CloudTrail `CreateWorkgroup` | `redshift-serverless-workgroup-tagging-rule` → Redshift Serverless state machine |
| RDS service event `RDS-EVENT-0170` / `RDS-EVENT-0005` | `rds-tagging-on-ready-rule` → RDS Lambda → tag `detail.SourceArn` |

Every CloudTrail-driven rule above also requires `errorCode` to be **absent**.
CloudTrail records rejected API calls too, and a rejected `Create*` left nothing
to tag: without that filter a failed call would make the generic Lambda tag an
ARN that does not exist, and would make a polling workflow poll a resource that
never appears until the state machine times out. The RDS rule needs no such
filter — it listens to a service event that is only emitted on success.

All five polling state machines share one shape:

```
Prepare (Lambda: parse event -> ids + tags)
   |
Check (Lambda or Describe*) <-------+
   |                                |
IsReady? --- no --> Wait(jitter) ---+
   |
  yes --> AddTags --> Complete      (AddTags exhausts retries --> Failed)
```

- **Generic services** are tagged immediately by the existing Lambda: EC2/EBS,
  S3, Lambda, DynamoDB, ELB, EFS, SQS, SNS, KMS, GameLift, CloudWatch Logs, MSK,
  EKS, ECS (cluster + service), Managed Service for Apache Flink, and Redshift
  Serverless namespaces. The `aws.es`, `aws.rds`, `aws.elasticache`,
  `aws.kinesis` and `aws.redshift` sources are **not** on this rule.
- **OpenSearch** polls `DescribeDomain` until `Processing == false`, then calls
  `AddTags`.
- **ElastiCache** (cache cluster, replication group, serverless cache) polls
  `Describe*` until `Status == available`, then calls `AddTagsToResource`. The
  first retry waits a randomized 60–120s to spread `Describe*` across a batch.
- **Kinesis Data Streams** polls `DescribeStreamSummary` until `ACTIVE`, then
  calls `AddTagsToStream`. `CreateStream` returns an empty body, so both the
  stream name and the ARN are recovered from the describe call, and the retry
  wait is a shorter 15–45s because a stream normally goes ACTIVE in seconds.
- **Redshift (provisioned)** polls `DescribeClusters` until `available`, then
  calls `CreateTags`. `DescribeClusters` returns no ARN, so the prepare step
  builds it from the event's account/region.
- **Redshift Serverless workgroups** poll `GetWorkgroup` until `AVAILABLE`, then
  call `TagResource`.
- **RDS / Aurora** (and DocumentDB, which shares the `aws.rds` event stream) are
  tagged by a Lambda triggered by the RDS service event once the resource is
  created — `RDS-EVENT-0170` (DB cluster created) and `RDS-EVENT-0005` (DB
  instance created) — using `detail.SourceArn` directly.

> **Note on `identityRecording` for RDS/Aurora:** the RDS service event carries
> no `userIdentity`, so the requester identity cannot be recorded for RDS/Aurora
> resources. Every other path is triggered by a CloudTrail Create event, which
> does carry `userIdentity`, so `identityRecording` works there.

## Redshift Serverless: one event source, two paths

`aws.redshift-serverless` appears on **two** EventBridge rules, split by
`eventName`:

- `CreateNamespace` → generic Lambda. A namespace is `AVAILABLE` as soon as the
  API returns, so there is nothing to wait for.
- `CreateWorkgroup` → its own state machine. A workgroup spends minutes in
  `CREATING`, and tagging during provisioning is not documented as supported.

The two event names are disjoint, so the rules never both fire for one resource.

> Note the IAM/SDK naming split: the IAM action prefix is `redshift-serverless:`
> (with a dash) while the Step Functions AWS SDK integration service is
> `redshiftserverless` (without). The state machine therefore passes
> `iam_action` explicitly.

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
2. Python 3.10+ and Node.js installed. Deploying on its own works on 3.9
   (cdk-nag's floor), but the pinned pytest in `requirements-dev.txt` needs
   3.10+, so 3.10 is the practical minimum.

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
