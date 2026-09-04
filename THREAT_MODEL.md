# Threat Model — sample-auto-tag-aws-resources

> For PCSR ticket. Follows the Simplified AWS SA Threat Model template
> (System → Data flow → Assumptions → Threats & Mitigations → Open items).
> This document is review evidence; it does not need to ship in the public repo.

## 1. System description

An event-driven AWS CDK (Python) sample that automatically applies a
deploy-time-configured set of tags to **newly created** AWS resources in the
account/region where it is deployed. Three trigger paths:

1. **Synchronous tagging (generic services)** — Amazon EventBridge matches
   CloudTrail `Create*` events for EC2, S3, Lambda, DynamoDB, ELB, EFS, SQS,
   SNS, KMS, GameLift, CloudWatch Logs, MSK, EKS, ECS (cluster + service),
   Managed Service for Apache Flink, and Redshift Serverless namespaces →
   invokes a Lambda that tags the resource immediately. Glue (`aws.glue`) and
   Athena (`aws.athena`) reach the **same** Lambda via a second rule, kept
   separate only so Glue's untaggable `CreateTable` events never invoke it.
2. **Polling tagging (slow to provision)** — EventBridge matches OpenSearch
   (`aws.es`), ElastiCache (`aws.elasticache`), Kinesis Data Streams
   (`aws.kinesis`), Redshift (`aws.redshift`), and Redshift Serverless
   workgroups (`aws.redshift-serverless` / `CreateWorkgroup`) create events →
   starts a Step Functions state machine that polls `Describe*`/`Get*` until the
   resource is ready, then applies tags.
3. **Service-event tagging (RDS/Aurora)** — EventBridge matches the native RDS
   service events `RDS-EVENT-0170` / `RDS-EVENT-0005` → invokes a Lambda that
   tags `detail.SourceArn`.

Optional **identity recording** mode records the requesting principal
(`userId`, and `roleId` for assumed roles) as a tag, sourced from the
CloudTrail event's `userIdentity` (not available on the RDS service-event path).

## 2. Trust boundaries & data flow

```
CloudTrail / RDS service events (AWS-internal, trusted)
        │
   Amazon EventBridge (account-local rules)         ← trust boundary: only
        │                                              AWS-emitted events match
   ┌────┴───────────────┬──────────────────┐
 Lambda            Step Functions       Lambda
 (generic tag)     (5 poll+tag flows)   (RDS tag)
        │                │                  │
   tagging APIs on the account's own resources (same account, same region)
```

- **No external/customer input.** All inputs originate from AWS-emitted
  CloudTrail/EventBridge events within the same account. There is no public
  endpoint, no API Gateway, no user-supplied payload.
- **Tag values** are operator-supplied at deploy time via a CDK/CloudFormation
  parameter (`tags`), stored as a Lambda environment variable. Non-secret.
- **Same-account, same-region** only. The roles cannot act cross-account.

## 3. Assumptions

- Deployed by an account administrator into an account they control.
- CloudTrail is enabled (required for the `Create*` events to flow).
- Tag keys/values supplied are not sensitive (documented in README).
- This is **sample/non-production** code; consumers harden before production
  (disclaimer included in README per PCSR Q12).

## 4. Threats & mitigations

| # | Threat (STRIDE) | Mitigation |
|---|---|---|
| T1 | **Elevation / over-broad IAM** — Lambda roles use `resources=["*"]` | **Inherent & justified**: the automation must tag resources whose ARNs are unknown at deploy time (any future resource). Actions are scoped to **tag-write + read-only `Describe*`/`Get*Tagging`/`List*Tags` only** — no create/delete/modify of data or infra, and (after T8) no tag-removal. The granted action set is exactly what the handler code exercises. Each trigger path has its **own least-privilege role** (generic / RDS / OpenSearch / ElastiCache / Kinesis / Redshift / Redshift Serverless split), so a fault in one path cannot tag-API another service set. The Glue and Athena grants are `glue:TagResource`, `athena:TagResource`, plus the one read Glue's own tag path forces (`glue:GetDatabase`, metadata only) — no `GetTable*`/`Search*`, so the role cannot read table schemas or query metadata. |
| T9 | **Credential disclosure via Glue connection tagging** — tagging a `connection` ARN makes Glue run an internal existence check that requires `glue:GetConnection`, and `GetConnection` with `HidePassword=false` returns the connection's **plaintext `PASSWORD`** property. Granting it would give the tagging role read access to JDBC credentials. | **Avoided by scope.** Glue connections are excluded from `_GLUE_RESOURCES` and from the `glue-athena-tagging-rule` event pattern, so the role never needs `glue:GetConnection` and it is not granted. A connection is unbilled configuration, so nothing cost-allocation-relevant is lost. Regression-tested (`test_generic_role_can_read_glue_databases_but_not_connections`). The one read that *is* granted, `glue:GetDatabase`, returns database metadata (name, description, S3 location URI) and no secrets. |
| T2 | **Wildcard action** `resource-groups:*` in the generic role | **Remediation: removed.** The code only calls `resourcegroupstaggingapi.tag_resources` (= `tag:TagResources`, already granted). The unused `resource-groups:*` wildcard was deleted. |
| T3 | **Tampering via spoofed events** | EventBridge rules match only AWS-native `source`/`detail-type`/`eventName`; events are produced by CloudTrail/RDS, not externally injectable. No public ingress. |
| T4 | **Info disclosure via identity recording** | Off by default (`identityRecording=false`). When on, it records only principal IDs (`userId`/`roleId`) as tags — no credentials/secrets. Documented in README. |
| T5 | **DoS / runaway polling** | Step Functions executions have a 2-hour `timeout`; `Describe*`/`Get*` calls use bounded retries with backoff + full jitter; the retry wait is randomized per execution (60–120s, or 15–45s for Kinesis, which normally goes ACTIVE within seconds) to spread describe calls across a batch. The two in-Lambda retries (ECS service, MSF application) are bounded at 5 attempts × 3s and only fire on named eventual-consistency error codes. |
| T6 | **Logging exposure** | State machine logs go to a dedicated CloudWatch Log group with 1-month retention; logs contain resource IDs/ARNs and tag keys/values only (no secrets). |
| T7 | **Repudiation** | All actions are CloudTrail-logged under the function's role; Step Functions execution history retained. |
| T8 | **Unused / mutating grants in the generic role** — the policy granted `tag:UntagResources`, `cloudformation:DescribeStacks`, `cloudformation:ListStackResources`, `ec2:DescribeNatGateways`, `ec2:DescribeInternetGateways`, none of which any handler calls | **Remediation: removed.** Verified by code review that no Lambda path invokes them; `tag:UntagResources` is *mutating* (can strip tags) and contradicted the "no modify" scope in T1. Removing them makes the granted policy exactly equal to what the code exercises. The retained reads (`ec2:DescribeVolumes`, `dynamodb:DescribeTable`, S3/SQS/KMS/EFS `Get*Tagging`/`List*Tags`) are required by the Resource Groups Tagging API / boto3 waiters and were kept. |

## 5. Open items / Guardian discussion points

- **`resources=["*"]` (T1) — ACCEPTED.** The read-only + tag-only action
  scoping is accepted for this tagging-automation sample. `resources=["*"]` is
  inherent: the automation tags resources whose ARNs are unknown at deploy time.
  No data-plane or destructive permissions are granted; each trigger path has
  its own least-privilege role.
- **AppSec escalation clause (Appendix C) — DOES NOT APPLY.** Confirmed: **no**
  PII, **no** cryptography, **no** auth, **no** third-party/GenAI, **no**
  customer data. The optional `identityRecording` mode records only the IAM
  principal ID (`userId`/`roleId`) of the operator who created the resource —
  an operational principal identifier, opt-in (default off), with no
  credentials/secrets — and is not treated as escalation-triggering identity
  data.

## 6. Scanner evidence

- [x] **GitLab Probe rescan (main, 2026-06-23)** — exported and attached to
  ticket. **No high/critical; no actionable findings.** The only remaining
  entry is one INFO that requires manual confirmation (see below). This rescan
  followed the remediation of every finding from the initial scan.

| Finding | Tool / sev | Disposition |
|---|---|---|
| "Detected a CDK project with AwsSolutionsCheck, however we cannot determine whether the project actually consumes cdk_nag." | cdk_nag_detect / **INFO** | **Verified manually (scanner limitation).** The scanner sees `AwsSolutionsChecks` imported/instantiated but cannot statically confirm the aspect is attached to the app. It is: `app.py` does `cdk.Aspects.of(app).add(AwsSolutionsChecks(...))`, and `cdk synth` runs the rules and exits 0 with **0 unsuppressed findings** — proof cdk_nag is active. |

### Initial scan (2026-06-22) — remediation audit trail

The first Probe scan surfaced four findings (no high/critical). All were fixed
or justified; the 2026-06-23 rescan above confirms they no longer appear.

| Finding | Tool / sev | Location | Disposition |
|---|---|---|---|
| `globals()` dynamic dispatch | semgrep / WARNING (`dangerous-globals-use`) | `lambda/lambda-handler.py` | **Fixed.** Replaced `globals()[source]` with an allowlisted dispatch dict (`_SOURCE_HANDLERS`); unknown sources are rejected. Defense-in-depth for T3. |
| Non-crypto PRNG | bandit / INFO (`B311`) | `lambda/elasticache_tagging.py` | **Justified (false positive).** `random.randint` is polling jitter (T5), not security/crypto. Annotated `# nosec B311`. |
| cdk_nag not in use | cdk_nag / ERROR | CDK app | **Fixed.** Added `AwsSolutionsChecks` aspect in `app.py` + `cdk-nag` dependency. `cdk synth` now runs clean (0 unsuppressed findings). A unit test (`test_no_unsuppressed_cdk_nag_findings`) now asserts this on every change, because `App.synth()` alone does not fail on error annotations — only the `cdk synth` CLI reports them. |
| IAM5 / IAM4 / SF2 (surfaced after enabling cdk_nag) | cdk_nag | IAM roles & state machines | **Resolved + justified.** L1 (old runtime) fixed by bumping all Lambdas to Python 3.13. IAM5 wildcard (tag deploy-time-unknown ARNs, tag/read-only — T1/T8), IAM4 (standard Lambda logging policy), and SF2 (X-Ray = observability, not security) each carry a scoped `NagSuppressions` entry with documented evidence in the stack. |

- [ ] (Optional) Slingshot scan — run if required by the PCSR reviewer; Probe
  results above are the primary evidence.
