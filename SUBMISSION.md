# NoMeshOps — hackathon submission

**Event:** WeMakeDevs x AWS "First Commit" hackathon · Ship It track · Bharat Builds Tour
**Repo:** https://github.com/SibgathKhan777/NoMeshOps · **Sample target:** https://github.com/SibgathKhan777/nomeshops-sample

## What it is

A deployment agent that fingerprints a target machine, tries to deploy a Python project on it, and when the deploy
fails, fixes it. It consults a **deterministic rules table first**, then a **DynamoDB knowledge base of previously
verified fixes**, and only for a genuinely new failure asks **Claude on Amazon Bedrock** for one structured fix.
Every fix must pass a real verification chain (install → import → the app actually starts → HTTP health check) before
it is stored. The same error therefore resolves near-instantly the next time it is seen, on any machine with the same
runtime context. The system gets measurably more reliable with every error it resolves.

## What we measured on real AWS (ap-south-1, 2026-09-18/19)

| run | target | path | wall time |
|---|---|---|---|
| novel error | Ubuntu 22.04 (A) | pip fails in 10 s → rules miss → knowledge miss (253 ms) → Bedrock proposes a fix in 2.3 s → fix applied → install → import ok → app up → `/health` 200 → fix stored | **42 s** |
| same error again | Ubuntu 22.04 (B) | pip fails → rules miss → **knowledge hit in 301 ms** → same fix → verified → success_count 2 | **34 s, 0 LLM calls** |
| different platform | Amazon Linux 2023 | rules table predicted missing git/pip from the fingerprint and pre-installed them → verified first try | 58 s, 0 fixes |
| through the deployed service | Ubuntu 22.04 (B) via ECS Express Mode `POST /deploy/stream` | knowledge hit in **45 ms** from inside AWS → verified | 29.6 s |

These runs happened before the hardening described in the next section, so treat the wall times as indicative of the
shape of the loop rather than as post-fix benchmarks. AWS resources were then torn down to stop spend; the DynamoDB
table, S3 bucket, ECR image and IAM roles remain so a redeploy is three script runs.

## How we tested it, and what that found

We did not stop at "it worked in the demo". We built an adversarial evaluation harness (`evals/`) and ran the system
against cases designed to break it. Every number below is produced by a suite in that directory.

| suite | cases | result |
|---|---|---|
| end-to-end on real containers | 11 | 11 pass |
| orchestration scenarios against scripted targets | 11 | 11 pass |
| error type classification | 27 | 92.6% |
| package attribution / version attribution | 27 | 96.3% / 100% |
| pre-deploy rules, precision and recall | 20 | 1.00 / 1.00 |
| post-failure rule routing | 20 | 100% |
| requirements/pyproject parsing | 8 | 100% |
| destructive-command filter | 24 | 16/16 dangerous blocked, 0/8 benign blocked |
| held-out real-world pip logs | 3 | 100% |

The deterministic check averages **0.18 ms** over 1000 iterations, which is what makes "rules before the model"
free rather than merely tidy.

**The evaluation found six real defects, all fixed in commit `cc79ebb`.** The most important one struck at our own
headline claim:

1. **False "verified".** The health check never proved that the process answering on the port was the one it had just
   started. A leftover server from an earlier run answered every probe, and four end-to-end scenarios passed for the
   wrong reason, two of which were written specifically to fail. Three faults compounded: `fuser` is absent from both
   target images so the port-clearing line was a silent no-op, the cleanup scan matched a virtualenv path that never
   appears in a process command line because `argv[0]` is just `python`, and nothing compared responder identity.
   Verification now resolves port owners through `/proc/net/tcp` → `/proc/*/fd`, clears stale listeners, refuses to
   continue if the port cannot be freed, and fails if the responder is not the process it launched.
2. **Knowledge-base poisoning.** A pip read timeout followed by a successful retry taught the system a permanent
   "verified fix": the network had simply recovered, and whatever ran in between took the credit. Transient
   infrastructure failures are now a distinct class, retried unchanged, never sent to the model, never learned.
3. **Signature fragmentation.** The package version was read only from the orchestrator's own clone of the repo, so
   the same error on the same host hashed differently when that clone was unavailable, splitting knowledge-base rows
   and success counters. The version is now read from pip's own output first.
4. **History loss on re-learn.** Writing a new fix overwrote the row, resetting `failure_count` and `first_seen_at`.
   Both backends now preserve history and record what a new fix superseded.
5. **Model budget spent on non-defects.** A bad repository URL matched the `not found` substring intended for a
   missing git binary, and an unreachable target ran the whole fix ladder. Both now stop immediately.
6. **Deny-list evasion.** `rm -rf --no-preserve-root /` slipped past a rule written to catch `rm -rf /`. The matcher
   now tolerates interleaved flags.

Four of those six share one shape: **the system credited an observation it had not attributed.** A fix preceded a
success, so the fix got the credit; a port answered, so the app was assumed healthy. That is the characteristic
failure mode of anything that learns from its own outcomes, and it is the thing we now design against explicitly.

### Does it generalise beyond our own demo?

- **Across projects.** A fix learned on the sample project was applied to an unrelated project that shared only the
  faulty dependency: exact knowledge-base hit in 1 ms, counter incremented, no duplicate row.
- **Across platforms.** A fix learned on Ubuntu 22.04 with Python 3.10 resolved the same error class on Amazon Linux
  2023 with Python 3.9 through the family index, with verification still acting as the gate.
- **Across other people's logs.** Three pip failure logs copied verbatim from public issue trackers (psycopg2,
  Unity ml-agents, mysqlclient), one of them from Windows and one from a decade-old pip output format, all produced
  the correct error type, package and fix routing.

## AWS services used, and why

| service | role in the system |
|---|---|
| **Amazon ECS Express Mode** | Hosts the FastAPI + LangGraph orchestrator. One `create-express-gateway-service` call gave us a load-balanced HTTPS URL with an ECS task role, no ALB/target-group/security-group ceremony. (App Runner stopped taking new customers in April 2026; Express Mode is the current equivalent.) |
| **AWS Systems Manager Run Command** | Every interaction with a target machine: fingerprint, deploy, verify. No SSH keys, no inbound ports, auditable command history, works across Ubuntu / Amazon Linux / x86 / Graviton. Targets only need the `AmazonSSMManagedInstanceCore` policy and a tag. |
| **Amazon DynamoDB** | The knowledge base. Partition key `error_signature` (hash of error type + package + version + OS + arch + Python minor + normalized message); a `family-index` GSI on the context-free signature gives a second chance across platforms, with verification as the gate. On-demand, sub-second, exact match — no vector search needed to prove the "gets smarter" story. |
| **Amazon Bedrock (Claude, Converse API)** | Fix generation, only on a knowledge-base miss. One JSON object per call under a strict execution contract; a deny-list rejects destructive commands before anything reaches SSM. Model: `global.anthropic.claude-sonnet-4-6`. |
| **Amazon S3** | Audit trail: one JSON record per run (fingerprint, scan, predicted issues, each attempt with raw output tails, fix chain with its source, verification results, event timeline). SSM also streams full command output there. |
| **Amazon ECR, IAM, CloudWatch Logs** | Image registry, least-privilege roles (task role is scoped to tagged instances, one table, one bucket), container logs. |

### Where the $200 credit actually goes

Checked against the hackathon's Ship It free-tier list (SageMaker AI; EKS/ECS/Fargate; Lambda/API Gateway/Step
Functions; EC2/Lightsail/App Runner/Amplify Hosting; S3/DynamoDB/RDS/Aurora; Cognito; CloudFront/Route
53/EventBridge/SQS/SNS/CloudWatch): every service above is on that list except Bedrock, SSM, ECR, and IAM. That
split matters for budgeting, not just compliance:

- **Bedrock is the only real cost driver.** It has no free tier at all — every fix-generation call is billed per
  token from the first request, unlike ECS/S3/DynamoDB/API Gateway/CloudWatch, which stay inside their standing free
  tiers at demo scale. This is why the system treats a model call as a last resort (deterministic rules, then the
  knowledge base, then Bedrock) and caps it at one call per run — it is a budget constraint as much as a design one.
  `LLM_BACKEND=anthropic` (direct API) or `LLM_BACKEND=none` (deterministic + knowledge base only) are the fallbacks
  if credits run low before the demo.
- **SSM Run Command isn't billed separately** — only the underlying EC2 instance is, so it's absent from the list
  without being a cost concern.
- **ECR** carries its own small free tier (500MB / 12 months), trivial for one container image.
- **IAM** is always free.

## Design choices an infra reviewer will care about

- **Deterministic before probabilistic.** The rules table runs in well under a millisecond, touches no network, and
  caught every post-failure case it should have across 20 labelled samples. The model is the last resort, not the first.
- **Verification is the gate, not the exit code.** A fix is only "known" after `pip install` succeeds, every declared
  package imports, the app process starts, the health endpoint answers, **and the process that answered is the one we
  started.** Each attempt begins from a fresh clone and a fresh virtualenv.
- **Runtime context is part of the error identity.** The same error text on Python 3.9 and 3.12 is not the same
  problem, so the signature encodes OS, architecture and Python minor. A family index lets a fix travel across
  platforms, but only through the same verification gate.
- **Not everything is a defect to fix.** Transient network failures, busy targets, unreachable targets and port
  conflicts are classified as infrastructure conditions: they are retried or reported, never learned, and never
  spend model budget.
- **Hard stop conditions.** At most 3 fix attempts and 1 model-assisted retry per run, then a structured failure
  report with the full attempt history. A per-target lock makes concurrent deploys on one host fail fast instead of
  corrupting each other.
- **Fixes run under an explicit contract.** Stage `pre` runs before `git clone` for system bootstrap; stage `post`
  runs after the virtualenv exists and before `pip install`. Model output is validated against a destructive-pattern
  deny-list, which is a backstop against a careless model rather than a sandbox against an adversary; the real
  containment is that targets are disposable.

## Scope, honestly

Built and tested against EC2 instances we control, tagged `nomeshops=target`, in one account. Not arbitrary customer
infrastructure, not cross-account, no auth on the API, no dashboard. Deliberately cut, with reasons recorded in the
build plan: Cognito, Amplify, Step Functions/EventBridge, Strands, pgvector, CodeBuild sandboxing, CDK, Secrets
Manager, Bedrock AgentCore.

What the evaluation does **not** cover: the post-fix code has been verified in local Docker mode only, because AWS was
torn down to stop spend, so the Bedrock path has not been re-exercised since the fixes; all targets were Linux with
Python and pip; the sample applications are single-file web services; and three real-world logs is a small,
directional sample. The three phase smoke tests plus `evals/` are the re-validation plan when credits arrive.

## AI coding tools used

Claude Code wrote the implementation, the tests, the scripts, the evaluation harness and this document under the
author's direction and review, and drove the real-AWS runs above (Claude Fable 5.1 for the build, Claude Opus 5 for
the evaluation pass). Amazon Bedrock with Claude Sonnet 4.6 is the runtime model inside the product itself. All
commits are co-authored accordingly.

## Running it

See `README.md`. On AWS: `scripts/provision.sh` → `scripts/launch_targets.sh` → `python -m cli.demo deploy …` →
`scripts/deploy_ecs_express.sh`. With no AWS account at all, local mode runs the identical loop against Docker
containers: `cp .env.local.example .env && ./scripts/local_targets.sh`. Tests: `pytest` for the unit suite, and
`evals/components.py`, `evals/graph_scenarios.py`, `evals/realworld_logs.py`, `evals/poisoning.py`,
`evals/e2e_local.py` for the evaluation suites above.
