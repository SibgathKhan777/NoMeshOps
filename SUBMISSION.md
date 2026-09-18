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

## What we measured (real AWS, ap-south-1, 2026-09-18/19)

| run | target | path | wall time |
|---|---|---|---|
| novel error | Ubuntu 22.04 (A) | pip fails in 10 s → rules miss → knowledge miss (253 ms) → Bedrock proposes a fix in 2.3 s → fix applied → install → import ok → app up → `/health` 200 → fix stored | **42 s** |
| same error again | Ubuntu 22.04 (B) | pip fails → rules miss → **knowledge hit in 301 ms** → same fix → verified → success_count 2 | **34 s, 0 LLM calls** |
| different platform | Amazon Linux 2023 | rules table predicted missing git/pip from the fingerprint and pre-installed them → verified first try | 58 s, 0 fixes |
| through the deployed service | Ubuntu 22.04 (B) via ECS Express Mode `POST /deploy/stream` | knowledge hit in **45 ms** from inside AWS → verified | 29.6 s |

The verification gate also earned its keep: an early run's import check used the pre-fix manifest, flagged a fix
that had actually worked as unverified, stored nothing, and reported a clean failure. That bug was fixed; the gate
never let a bad record into the knowledge base.

## AWS services used, and why

| service | role in the system |
|---|---|
| **Amazon ECS Express Mode** | Hosts the FastAPI + LangGraph orchestrator. One `create-express-gateway-service` call gave us a load-balanced HTTPS URL with an ECS task role, no ALB/target-group/security-group ceremony. (App Runner stopped taking new customers in April 2026; Express Mode is the current equivalent.) |
| **AWS Systems Manager Run Command** | Every interaction with a target machine: fingerprint, deploy, verify. No SSH keys, no inbound ports, auditable command history, works across Ubuntu / Amazon Linux / x86 / Graviton. Targets only need the `AmazonSSMManagedInstanceCore` policy and a tag. |
| **Amazon DynamoDB** | The knowledge base. Partition key `error_signature` (hash of error type + package + version + OS + arch + Python minor + normalized message); a `family-index` GSI on the context-free signature gives a second chance across platforms, with verification as the gate. On-demand, sub-second, exact match — no vector search needed to prove the "gets smarter" story. |
| **Amazon Bedrock (Claude, Converse API)** | Fix generation, only on a knowledge-base miss. One JSON object per call under a strict execution contract; a deny-list rejects destructive commands before anything reaches SSM. Model: `global.anthropic.claude-sonnet-4-6`. |
| **Amazon S3** | Audit trail: one JSON record per run (fingerprint, scan, predicted issues, each attempt with raw output tails, fix chain with its source, verification results, event timeline). SSM also streams full command output there. |
| **Amazon ECR, IAM, CloudWatch Logs** | Image registry, least-privilege roles (task role is scoped to tagged instances, one table, one bucket), container logs. |

## Design choices an infra reviewer will care about

- **Deterministic before probabilistic.** The rules table runs in well under a millisecond, touches no network, and
  catches the boring-but-common cases (missing git/pip/venv/compiler, requires-python mismatches, packages whose new
  majors need a newer Python, source builds that need system headers). The LLM is the last resort, not the first.
- **Verification is the gate, not the exit code.** A fix is only "known" after `pip install` succeeds, every declared
  package imports, the app process starts, and the health endpoint answers. Each attempt starts from a fresh clone and
  a fresh venv, so a verified result is a real reproduction.
- **Runtime context is part of the error identity.** The same error text on Python 3.9 and 3.12 is not the same
  problem; the signature encodes OS, architecture and Python minor. A family index still lets a fix travel across
  platforms, but only through the same verification gate.
- **Hard stop conditions.** At most 3 fix attempts and 1 LLM-assisted retry per run, then a structured failure report
  with the full attempt history. No indefinite looping.
- **Fixes run under an explicit contract.** Stage `pre` runs before `git clone` (system bootstrap), stage `post` runs
  after the venv exists and before `pip install` (manifest edits, pip upgrades). The model is told this contract and
  its output is validated against a destructive-pattern deny-list.

## Scope, honestly

Built and tested against EC2 instances we control, tagged `nomeshops=target`, in one account. Not arbitrary customer
infrastructure, not cross-account, no auth on the API, no dashboard. That is a deliberate MVP boundary. Deliberately
cut, with reasons in the build plan: Cognito, Amplify, Step Functions/EventBridge, Strands, pgvector, CodeBuild
sandboxing, CDK, Secrets Manager, Bedrock AgentCore.

## AI coding tools used

Claude Code (Claude Fable 5.1) wrote the code, the tests, the scripts and this document under the author's direction
and review; it also drove the real-AWS runs above. Amazon Bedrock (Claude Sonnet 4.6) is the runtime model inside the
product. All commits are co-authored accordingly.

## Running it

See `README.md`: `scripts/provision.sh` → `scripts/launch_targets.sh` → `python -m cli.demo deploy …` →
`scripts/deploy_ecs_express.sh`. Offline tests: `pytest`. A Docker-based local mode (no AWS account needed) is
described in the README under "Local mode".
