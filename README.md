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
| Glue database, crawler, job, trigger, workflow, interactive session | Data Catalog / orchestration objects, taggable as soon as `Create*` returns |
| Athena workgroup, data catalog, capacity reservation | created synchronously |
| Redshift Serverless **namespace** | AVAILABLE on creation (no CREATING state) — the **workgroup** polls instead |

> Eight of these services cannot go through the Resource Groups Tagging API:
> their tag APIs disagree on parameter casing and tag structure (map vs
> upper-case `[{Key, Value}]` vs lower-case `[{key, value}]`, and Glue names the
> parameter `TagsToAdd` rather than `Tags`), so each has a small native tagger in
> `_NATIVE_TAGGERS`.

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
| CloudTrail Glue / Athena `Create*` | `glue-athena-tagging-rule` → same generic Lambda → tag now |
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
- **Glue and Athena** are tagged immediately by that same Lambda, but ride a
  second rule (`glue-athena-tagging-rule`) — see below for why.
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

## Glue and Athena: immediate tagging on a separate rule

Both services are tagged synchronously by the generic Lambda, but they are
matched by their own EventBridge rule rather than being folded into the shared
one. The shared rule ORs a single flat `eventName` list across every `source`,
and it already carries DynamoDB's `CreateTable`. Glue emits `CreateTable` too —
one per table a crawler discovers — so folding Glue in would invoke the Lambda
once per discovered table for a resource that cannot be tagged at all.

**What Glue can and cannot tag.** `glue:TagResource` accepts 16 ARN types:
`database`, `catalog/<name>`, `connection`, `crawler`, `job`, `trigger`,
`workflow`, `session`, `devEndpoint`, `mlTransform`, `usageProfile`, `registry`,
`schema`, `blueprint`, `dataQualityRuleset`, and `customEntityType`. It rejects
`table`, `tableVersion`, `partition`, `column`, `userDefinedFunction`, `jobRun`,
`crawl`, `classifier`, `securityConfiguration`, `script`, and
`dataCatalogEncryptionSettings` (all `InvalidInputException`), as well as the
**root** `catalog` with no name (*"Tag operations not supported on root
catalog"*) — the Data Catalog only supports tags down to the **database** level.

This solution covers the eight that carry or govern cost and can still be
created: `database`, `crawler`, `job`, `trigger`, `workflow`, `session`,
`mlTransform`, and `usageProfile`. All eight, plus all three Athena types, were
verified end-to-end in `us-east-1`: each resource was created untagged, and the
tags configured at deploy time appeared on it without any further action.

The unbilled remainder (`registry`, `schema`, `blueprint`,
`dataQualityRuleset`, `customEntityType`, and the named `catalog`) are one line
each in `_GLUE_RESOURCES` should you need them; the named `catalog` additionally
needs a `glue:GetCatalog` grant, for the same reason `database` needs
`glue:GetDatabase`.

> **An ML transform is addressed by ID, not by name.** Its ARN is
> `mlTransform/<TransformId>`, using the ID Glue generates, so the handler reads
> `responseElements.transformId` and deliberately does *not* fall back to the
> caller's chosen name — that would build an ARN pointing at nothing.

> **Why `devEndpoint` is left out even though it is billed.** AWS has retired
> Glue dev endpoints. The read APIs are switched off service-side —
> `GetDevEndpoint` answers *"operation is currently disabled"* and
> `ListDevEndpoints` returns `InternalFailure` — and `CreateDevEndpoint` accepts
> only Glue `0.9` and `1.0`, both long past end of support. The CloudTrail event
> can no longer be produced, so a handler branch for it could never be reached
> or verified. `tests/unit/test_generic_dispatch.py` asserts the no-op to keep
> the omission deliberate.

No Glue `Create*` API returns an ARN — most return an empty body and the rest
return just a name — so the ARN is rebuilt from the event's account and region,
the same way SNS and SQS are handled.

**Tagging a Data Catalog ARN needs a read grant too.** Tagging a Glue *database*
makes the service run an internal existence check, so `glue:TagResource` alone
fails with `AccessDeniedException: not authorized to perform: glue:GetDatabase on
resource: arn:aws:glue:…:catalog`. The role therefore also carries
`glue:GetDatabase`, which returns database metadata only. Only the three Data
Catalog *container* types behave this way — `database` needs `glue:GetDatabase`,
the named `catalog` needs `glue:GetCatalog`, and `connection` needs
`glue:GetConnection`. The other 13 types need nothing beyond
`glue:TagResource`.

> **Why Glue connections are out of scope.** A connection's existence check needs
> `glue:GetConnection`, and `GetConnection` with `HidePassword=false` returns the
> connection's **plaintext `PASSWORD` property**. Granting that to a tagging role
> would let it read JDBC credentials, which contradicts the "tag-write plus
> non-secret reads only" scope this sample claims (THREAT_MODEL.md T1). A
> connection is unbilled configuration, so the trade is not worth it. If you
> accept the risk, add `CreateConnection` back to `_GLUE_RESOURCES`, the
> EventBridge rule, and the role.

> **Watch the casing:** Athena emits `CreateWork`**`G`**`roup` while Redshift
> Serverless emits `CreateWorkgroup`. EventBridge matches `eventName` exactly, so
> the two rules never cross over — do not "normalize" either spelling.

> Glue's tag API is the odd one out twice over: the parameter is `ResourceArn`
> with a **`TagsToAdd` map**, not a `Tags` list. Athena uses `ResourceARN`
> (all-caps) with an upper-case `[{Key, Value}]` list, like Managed Service for
> Apache Flink.

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
$ sudo npm install -g aws-cdk
```

A global install writes into a root-owned prefix such as
`/usr/lib/node_modules`, so without `sudo` this usually fails with
`EACCES: permission denied`. If you would rather not install anything globally,
skip this step and prefix the `cdk` commands below with `npx` (`npx cdk synth`,
`npx cdk deploy …`) — `npx` fetches the CLI on demand and needs no elevation.

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
