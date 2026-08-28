# Melbourne Agent Village — Infrastructure & Architecture Security Review

**Scope:** `infra/lib/village-stack.ts`, `infra/bin/village.ts`, `infra/cdk.json`
**Account/Region:** 490004615937 / ap-southeast-2 (image-gen in us-west-2)
**Reviewer role:** Cooperative infrastructure reviewer
**Date:** 2026-08-28
**Framework:** AWS Well-Architected (Security + Reliability pillars), validated against current (2025/2026) AWS guidance.

> Line references are to `infra/lib/village-stack.ts` unless otherwise noted. Line numbers are approximate to the version reviewed; anchor points are quoted for durability.

---

## Executive summary

The stack is, on balance, thoughtfully constructed: DynamoDB IAM is scoped to explicit table + index ARNs, the SPA bucket is private behind CloudFront OAC, `enforceSSL` and `BLOCK_ALL` public access are set on both buckets, S3 is encrypted, tokens are short-lived (1h), and the Cognito password policy is strong. The comment block even claims "There are NO wildcard resource entries" — but that claim is **incorrect** (see HIGH-1).

The material gaps cluster around **edge/network exposure** (Fargate on public subnets, no WAF, CORS `*`), **observability** (no access logging anywhere: API GW, CloudFront, S3), and **IAM blast radius** (`anthropic.*` model wildcards). None are exotic; all are standard Well-Architected hardening items. Below, severity reflects a public-internet-reachable multi-tenant AWS account, discounted where the "dev demo" framing genuinely reduces impact.

| # | Severity | Finding |
|---|----------|---------|
| CRIT-1 | CRITICAL | Fargate engine task on **public subnet with public IP** + default (all-egress, internet-exposed) security group |
| HIGH-1 | HIGH | Bedrock IAM uses `anthropic.*` **resource wildcards** (API + Asset Lambdas) — contradicts the stack's own "no wildcards" claim |
| HIGH-2 | HIGH | **No AWS WAF** on CloudFront or API Gateway |
| HIGH-3 | HIGH | **API Gateway CORS `allowOrigins: ['*']`** with `Authorization` header allowed |
| HIGH-4 | HIGH | **No API Gateway throttling** (default route rate/burst limits unset) |
| MED-1 | MEDIUM | **No access logging** — API GW, CloudFront, and both S3 buckets |
| MED-2 | MEDIUM | **CloudFront has no security response-headers policy** (HSTS/CSP/X-Content-Type-Options etc.) |
| MED-3 | MEDIUM | **Assets S3 bucket CORS `allowedOrigins: ['*']`** with `allowedHeaders: ['*']` |
| MED-4 | MEDIUM | **No explicit VPC security group / egress restriction** for the Fargate service |
| MED-5 | MEDIUM | **DynamoDB PITR disabled in dev**; no DynamoDB deletion protection anywhere |
| MED-6 | MEDIUM | **No deletion protection** on Cognito User Pool / CloudFront / critical stateful resources |
| MED-7 | MEDIUM | **`ALLOW_ANON` ternary is dead code** (`isDev ? 'false' : 'false'`) — signals an anon-bypass path in the API |
| LOW-1 | LOW | Cognito **MFA not enabled**; `userPassword` (USER_PASSWORD_AUTH) flow enabled |
| LOW-2 | LOW | **CloudWatch Logs not KMS-encrypted**; only engine has an explicit log group/retention |
| LOW-3 | LOW | 30-day **refresh-token validity** with no token revocation strategy for baked-in operator creds |
| LOW-4 | LOW | **No `enforceSSL`/TLS floor** notes: CloudFront viewer min TLS not pinned; no `minimumProtocolVersion` |
| LOW-5 | LOW | **`fromLookup` default VPC** couples the stack to ambient account state (reproducibility/Reliability) |

---

## CRITICAL

### CRIT-1 — Fargate engine runs in a PUBLIC subnet with a public IP and default security group

**Where:** `EngineService`, ~lines 470–486 (and VPC lookup ~lines 449–452).

```ts
const vpc = ec2.Vpc.fromLookup(this, 'EngineVpc', { isDefault: true });
...
const engineService = new ecs.FargateService(this, 'EngineService', {
  ...
  assignPublicIp: true,
  vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
  ...
});
```

**Why it's a problem:**
- The task ENI receives a **routable public IP on an Internet Gateway subnet**. Combined with the default security group CDK attaches (no `securityGroups` provided), the task's network interface is directly on the internet. Any listening port or a compromised dependency is reachable/exfiltratable without a NAT boundary.
- Current AWS guidance (ECS "Connect applications to the internet" and the VPC/ECS best-practices literature) is explicit: **Fargate tasks that only need *outbound* access should run in private subnets** with egress via NAT gateway or, cheaper, **VPC interface/gateway endpoints** (ECR api/dkr, S3 gateway, CloudWatch Logs, STS, Bedrock). Public subnet + public IP is the pattern reserved for tasks that must *serve* inbound internet traffic — this engine serves none; it only calls DynamoDB, Bedrock, and AgentCore outbound.
- This directly violates Well-Architected Security **SEC05 (network protection / layered defense)** — the workload has no network isolation boundary between it and the internet.

The in-code comment justifies this by "this region is at its VPC / Internet Gateway limit." That is an operational constraint, not a security rationale, and it has a clean fix (below) that needs **no new VPC/IGW**.

**Remediation:**
1. Preferred: create private subnets in the existing default VPC (or a dedicated minimal VPC with `natGateways: 0`) and add **VPC interface endpoints** for `ecr.api`, `ecr.dkr`, `logs`, `sts`, `bedrock-runtime`, `bedrock-agentcore` plus an **S3 gateway endpoint**. Then:
   ```ts
   assignPublicIp: false,
   vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }, // or PRIVATE_ISOLATED + endpoints
   securityGroups: [engineSg], // egress 443 to endpoints only
   ```
   Fargate does **not** require the ECS interface endpoint; it does require ECR + Logs reachability, satisfied by endpoints — so **no NAT and no extra IGW** is needed.
2. If private subnets are truly impossible short-term, at minimum attach an **explicit security group with no inbound rules and egress restricted to 443** (see MED-4) and treat this as a documented, time-boxed exception.

---

## HIGH

### HIGH-1 — Bedrock IAM policies use `anthropic.*` resource wildcards (contradicts the stack's own claim)

**Where:**
- Asset Lambda policy, ~lines 250–265:
  ```ts
  resources: [
    `arn:aws:bedrock:${region}::foundation-model/anthropic.*`,
    `arn:aws:bedrock:${region}:${account}:inference-profile/au.anthropic.*`,
    ...
  ],
  ```
- API Lambda policy, ~lines 330–340: same `anthropic.*` foundation-model + `au.anthropic.*` inference-profile wildcards.

**Why it's a problem:**
- The class-level docstring asserts *"There are NO wildcard resource entries."* Two of the four principals use `anthropic.*` and `au.anthropic.*` in the **resource** position. This is a correctness gap between documented intent and implementation.
- AWS's own least-privilege-for-Bedrock guidance (AWS Security Blog "Implementing least privilege access for Amazon Bedrock" / "Simplified model access") is explicit that broad model-ARN patterns mean **"any newly enabled model becomes instantly callable"** — the fix is to *pin foundation-model ARNs per environment*. A wildcard over the entire Anthropic model family lets a compromised Lambda invoke *any* current or future Anthropic model, which is both a **cost-abuse** and a **data-egress-via-prompt** vector. Restricting model ARNs is the *first cost control and first security control* for Bedrock callers.
- The `EngineTaskRole` (lines ~510–540) does this correctly — it enumerates exact `OPUS_FM`/`HAIKU_FM` foundation-model ARNs and specific inference-profile ARNs. The Lambdas should match that pattern.

**Remediation:** Replace the wildcards with the exact model/profile ARNs actually used:
- Asset Lambda text model: pin `.../foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0` (the `HAIKU_FM` already defined) + the specific `au.anthropic.claude-haiku-...` inference-profile ARN, across the member regions — reuse the `foundationModelArns`/`inferenceProfileArns` arrays already built for the engine.
- API Lambda moderation model: same, pinned to the single Haiku profile + FM ARNs. Then update the docstring so the "no wildcards" claim becomes true.

### HIGH-2 — No AWS WAF on CloudFront (or API Gateway)

**Where:** `SpaDistribution` (~lines 545–575) has no `webAclId`; `SimulationHttpApi` has no associated WAF.

**Why it's a problem:** The SPA distribution and the JWT-protected API are both directly internet-facing with no L7 filtering. There is no rate-based rule, no AWS Managed Rules (common exploits, bad inputs, IP reputation, anonymous-IP), and no bot control. Well-Architected SEC (and the 2025 re:Inforce CloudFront+WAF guidance) treat a WAF web ACL as the baseline edge control for public distributions. Without it, the only protection in front of the API is Cognito JWT auth + API GW account-level throttling, and the SPA has none.

**Remediation:** Create an `aws-wafv2` `CfnWebACL` (scope `CLOUDFRONT`, deployed in us-east-1 for CloudFront) with `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesAmazonIpReputationList`, and a rate-based rule; set `webAclId` on the distribution. Add a `REGIONAL` web ACL for the HTTP API stage. Keep it in count mode first, then enforce.

### HIGH-3 — API Gateway CORS allows any origin with credentials-bearing headers

**Where:** `httpApi` `corsPreflight`, ~lines 355–366:
```ts
corsPreflight: {
  allowOrigins: ['*'],
  allowMethods: [GET, POST, OPTIONS],
  allowHeaders: ['Authorization', 'Content-Type'],
},
```

**Why it's a problem:** `allowOrigins: ['*']` combined with allowing the `Authorization` header means **any website** can script authenticated cross-origin calls against the API using a token they possess. For an operator-only control plane (start/pause/stop/reseed the world), the allowed origin set is known and tiny — it should be exactly the CloudFront domain. The README itself flags CORS `*` as "fine for a dev demo… scope it to the CloudFront domain for production," which acknowledges this is a deliberate deferral, not a design choice. It remains a real exposure for anything past a throwaway demo.

**Remediation:** Set `allowOrigins: [\`https://${distribution.distributionDomainName}\`]` (or the custom domain). Because the distribution is created later in the file, either compute the domain via a custom domain/known value, or split CORS config to reference the distribution (reorder), or restrict at the WAF/edge. Do not combine `*` origin with `Authorization`.

### HIGH-4 — API Gateway has no throttling configured

**Where:** `SimulationHttpApi` / `addRoutes` — no `defaultRouteSettings` (throttling) and no stage-level rate/burst limits (~lines 353–412).

**Why it's a problem:** An HTTP API with no explicit throttling falls back to **account-level** limits (shared across every API in the account, 10k rps / 5k burst default). A single abusive client — or a loop bug in the SPA — can consume the account's shared budget and starve other APIs, and can drive Lambda concurrency + Bedrock invocations (real $) with no per-API ceiling. This is both a Reliability (REL) and cost/DoS concern, compounded by the Bedrock spend the API triggers on `POST /events` moderation.

**Remediation:** Add explicit stage throttling. In CDK v2, configure the default stage route settings, e.g.:
```ts
new apigwv2.HttpApi(this, 'SimulationHttpApi', {
  ...,
  defaultRouteSettings: { throttlingRateLimit: 20, throttlingBurstLimit: 40 },
});
```
Size to expected operator/poll traffic (the SPA polls `state` on a loop, so account for the poll interval × operators). Pair with the WAF rate-based rule.

---

## MEDIUM

### MED-1 — No access logging anywhere (API Gateway, CloudFront, S3 buckets)

**Where:**
- `SimulationHttpApi`: no `accessLogSettings` / log destination on the default stage.
- `SpaDistribution`: no `enableLogging` / `logBucket` (~lines 545–575).
- `AssetsBucket` (~lines 118–147) and `SpaBucket` (~lines 528–540): no `serverAccessLogsBucket` / `serverAccessLogsPrefix`.

**Why it's a problem:** With no access logs at the edge, at the API, or on the buckets, there is **no audit trail** for who accessed what — you cannot do incident forensics, detect scraping of presigned asset URLs, or investigate abuse. Well-Architected SEC04 (detection) treats access logging as foundational. Note `cdk.json` already enables `@aws-cdk/aws-s3:createDefaultLoggingPolicy` and `serverAccessLogsUseBucketPolicy`, so the wiring cost is low — the log target buckets just aren't configured.

**Remediation:** Create a dedicated, access-controlled log bucket (SSE, lifecycle-expiry, `BLOCK_ALL`), then:
- API GW: attach a CloudWatch Logs `accessLogSettings` with a structured JSON format on the `$default` stage.
- CloudFront: `enableLogging: true, logBucket, logFilePrefix` (or standard logging v2 to the log bucket).
- Both app buckets: `serverAccessLogsBucket` + prefix. (CloudTrail data events for the assets bucket is an even stronger option.)

### MED-2 — CloudFront serves no security response headers (HSTS, CSP, nosniff, frame-options)

**Where:** `SpaDistribution.defaultBehavior` (~lines 546–556) sets no `responseHeadersPolicy`.

**Why it's a problem:** The SPA is served with no `Strict-Transport-Security`, `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options`/`frame-ancestors`, or `Referrer-Policy`. Given the SPA bakes operator context and calls an authenticated API, missing HSTS (downgrade/MITM) and missing CSP/frame protection (clickjacking, injected-script exfiltration of tokens) are real. AWS's 2025 HSTS-across-services guidance and CloudFront best practice both recommend a Response Headers Policy at the edge.

**Remediation:** Attach a `ResponseHeadersPolicy` (or the managed `SECURITY_HEADERS`, extended with HSTS `max-age=31536000; includeSubDomains; preload` and a CSP tailored to the SPA's API/asset origins):
```ts
responseHeadersPolicy: new cloudfront.ResponseHeadersPolicy(this, 'SpaSecHeaders', {
  securityHeadersBehavior: {
    strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, preload: true, override: true },
    contentTypeOptions: { override: true },
    frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
    referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
    contentSecurityPolicy: { contentSecurityPolicy: "default-src 'self'; connect-src 'self' <api-domain>; img-src 'self' <assets-domain> data:", override: true },
  },
})
```

### MED-3 — Assets S3 bucket CORS is `allowedOrigins: ['*']` with `allowedHeaders: ['*']`

**Where:** `AssetsBucket.cors`, ~lines 137–146.

**Why it's a problem:** The in-code comment argues this is safe because presigned URLs are signature-controlled — which is *partly* true for authorization of the object fetch. But `*` origin + `*` headers still means any origin can read responses and probe the bucket's CORS surface, and it weakens defense-in-depth if a presigned URL leaks (e.g., via logs/referrer). The response is only ever consumed by the SPA, so the origin is knowable.

**Remediation:** Restrict `allowedOrigins` to the CloudFront/custom domain(s), and narrow `allowedHeaders` to what `fetch()` actually sends (typically none needed for a simple GET; `['*']` is unnecessary). Keep `GET`/`HEAD` only.

### MED-4 — No explicit security group / egress restriction for the Fargate service

**Where:** `EngineService` (~lines 470–486) provides no `securityGroups`.

**Why it's a problem:** Absent an explicit SG, CDK creates a default security group that **allows all outbound (0.0.0.0/0)**. On a public subnet with a public IP (CRIT-1), that is unrestricted egress from an internet-exposed ENI — ideal for data exfiltration if the container is compromised. Well-Architected SEC05 calls for controlling egress, not just ingress.

**Remediation:** Define an explicit `ec2.SecurityGroup` with `allowAllOutbound: false` and add only the egress the task needs (443 to the VPC endpoints' SG / to AWS service prefix lists). Attach it to the service. This pairs with the CRIT-1 private-subnet + endpoints fix.

### MED-5 — DynamoDB PITR disabled in dev; no deletion protection on the table

**Where:** `WorldStateTable`, ~lines 74–82: `pointInTimeRecovery: !isDev` and no `deletionProtection`.

**Why it's a problem:** PITR-off in dev is a defensible cost tradeoff, but combined with `removalPolicy: DESTROY` and `autoDeleteObjects` in dev, an accidental `cdk destroy` or logical-ID change silently and irrecoverably wipes world state. There is also **no `deletionProtection: true`** on the table in *any* environment, so even prod (RETAIN policy) can be deleted via console/API without a guard. Well-Architected REL09 (backup) and change-management favor an explicit protection flag on stateful stores.

**Remediation:** Set `deletionProtection: !isDev` (or always `true` for test/prod). Consider enabling PITR in `test` too, since it's the environment most likely to hold data worth recovering. Document the dev data-loss tradeoff explicitly.

### MED-6 — No deletion protection on Cognito User Pool and other stateful resources

**Where:** `OperatorUserPool` (~lines 344–360) — no `deletionProtection`; CloudFront distribution and log/asset buckets similarly rely only on `removalPolicy`.

**Why it's a problem:** Losing the User Pool destroys operator identities and forces re-provisioning; there is no `deletionProtection: cognito.DeletionProtection.ACTIVE`. RemovalPolicy protects against stack deletion but not against direct resource deletion or replacement-on-update.

**Remediation:** Set `deletionProtection: DeletionProtection.ACTIVE` on the User Pool (at least for test/prod), and confirm RETAIN + deletion protection on all stateful resources for non-dev.

### MED-7 — `ALLOW_ANON` env var is dead-code ternary — indicates an anonymous-bypass path in the API

**Where:** API Lambda env, ~line 300:
```ts
ALLOW_ANON: isDev ? 'false' : 'false',
```

**Why it's a problem:** The ternary evaluates to `'false'` in both branches — this is dead code. Its existence means the **API application code reads an `ALLOW_ANON` flag that, when `'true'`, bypasses authentication.** That is a latent auth-bypass switch: a future one-character edit (or a `cdk deploy` with an overridden env) flips the entire API to unauthenticated. Ship-time it is safe (always false), but it's a foot-gun that shouldn't exist in an internet-facing control plane, and the misleading ternary suggests it was intended to differ per-env at some point.

**Remediation:** Remove the `ALLOW_ANON` env var (and the corresponding bypass branch in `api/`) entirely, so authentication cannot be disabled by configuration. If an anon mode is genuinely needed for local mock testing, gate it behind a build that never ships to AWS, not a deploy-time env var. (This crosses into the API code review — flag to that reviewer.)

---

## LOW

### LOW-1 — Cognito MFA not enabled; USER_PASSWORD_AUTH flow allowed
**Where:** `OperatorUserPool` (~lines 344–360, no `mfa`) and `OperatorUserPoolClient` `authFlows: { userPassword: true, userSrp: true }` (~lines 364–374).
**Why:** No MFA on an operator control plane; `userPassword` (USER_PASSWORD_AUTH) transmits raw passwords to Cognito and is weaker than SRP-only. For operators who can start/stop/reseed the world, MFA is warranted.
**Remediation:** Set `mfa: cognito.Mfa.REQUIRED` (or `OPTIONAL` with enforcement) + `mfaSecondFactor: { otp: true }`. Drop `userPassword: true`, keep `userSrp` only. Reconciles with the README's note to move off baked-in creds toward interactive login.

### LOW-2 — CloudWatch Logs not KMS-encrypted; only engine has explicit retention
**Where:** `EngineLogGroup` (~lines 545–..) sets retention but no `encryptionKey`; Lambda log groups are auto-created with default retention (never expire) and no CMK.
**Why:** Logs may contain agent prompts/PII-ish narrative; default-encrypted but not customer-managed, and Lambda logs retain forever (cost + exposure). Well-Architected SEC08.
**Remediation:** Add explicit `logs.LogGroup` for each Lambda with `retention` and a shared KMS key; set `encryptionKey` on the engine group too.

### LOW-3 — 30-day refresh token + baked-in operator creds, no revocation strategy
**Where:** `userPoolClient` `refreshTokenValidity: cdk.Duration.days(30)` (~line 372); README notes creds baked into public JS.
**Why:** A leaked bundle grants 30 days of refreshable access with no rotation/revocation path. `enableTokenRevocation` isn't set.
**Remediation:** Shorten refresh validity for the SPA client, enable token revocation, and prioritize the README's own recommendation to replace embedded creds with interactive login.

### LOW-4 — CloudFront viewer TLS floor not pinned
**Where:** `SpaDistribution` — no `minimumProtocolVersion`.
**Why:** Defaults are reasonable but not pinned; 2025 CloudFront added `TLSv1.2_2025` and PQ key exchange. Explicit is better for audits.
**Remediation:** Set `minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021` (or newer available) and, if a custom domain is added, an ACM cert.

### LOW-5 — `Vpc.fromLookup(isDefault)` couples the stack to ambient account state
**Where:** ~lines 449–452.
**Why:** `fromLookup` caches context and depends on a default VPC existing with expected subnets — reduces reproducibility across accounts and can produce surprising synth results if the default VPC changes. Reliability/portability concern.
**Remediation:** Prefer an explicitly-defined minimal VPC (with private subnets + endpoints per CRIT-1), or pass the VPC id via context and document the dependency.

---

## Things done well (for balance)

- **DynamoDB IAM** scoped to exact table + GSI ARNs via a reusable statement factory (`dynamoRwStatement`) — textbook least privilege.
- **EngineTaskRole Bedrock** permissions enumerate exact FM + inference-profile ARNs across member regions — this is the pattern the Lambdas should copy (HIGH-1).
- **SPA bucket** private with **CloudFront OAC** (not legacy OAI), `BLOCK_ALL` public access, SSE, `enforceSSL`.
- **Assets bucket** `BLOCK_ALL` + `enforceSSL` + presigned-URL access model (no public objects).
- **Short-lived ID/access tokens (1h)** and a **strong password policy** (12 chars, all classes).
- **JWT authorizer deliberately excludes `OPTIONS`** so preflight isn't 401'd — a correct, well-reasoned choice (documented inline).
- **`self-signup disabled`**, email-only recovery — appropriate for an operator-only pool.
- Env validation in `bin/village.ts` (`dev|test|prod`) and `simulationId` length check; consistent tagging.
- **Container Insights** enabled on the ECS cluster; engine log group has explicit retention.

---

## Suggested remediation order

1. **CRIT-1** — move Fargate to private subnets + VPC endpoints + explicit egress SG (fixes MED-4 too).
2. **HIGH-1** — pin Bedrock model ARNs on both Lambdas.
3. **HIGH-3 / MED-3** — scope API GW + assets-bucket CORS to the CloudFront origin.
4. **HIGH-2 / HIGH-4** — WAF web ACLs (CloudFront + API) and API GW throttling.
5. **MED-1 / MED-2** — access logging everywhere + CloudFront security headers.
6. **MED-5 / MED-6** — deletion protection + PITR posture.
7. **MED-7** — remove the `ALLOW_ANON` bypass.
8. **LOW-1..5** — MFA, log encryption/retention, token revocation, TLS floor, explicit VPC.
