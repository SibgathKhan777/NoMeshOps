## Description (paste into the Description field, 0/512)

A self-healing deployment agent that doesn't just report why a deploy failed — it diagnoses the failure, fixes it, and verifies the fix for real, using a rules table, a DynamoDB knowledge base, and Amazon Bedrock only as a last resort.

---

## Body (paste into the Body field)

# NoMeshOps: a deployment agent that fixes its own failures

Every developer knows this moment: you deploy to a fresh machine, and it fails on something that
has nothing to do with your code. A missing system library. A package that needs to compile from
source and has no compiler. An app that installs cleanly and then crashes the instant it starts.
You SSH in, read the log, run one shell command, redeploy, and repeat — sometimes three or four
times before it works.

**NoMeshOps** is an agent that does that loop itself. You give it a target machine and a Python
repo. It deploys the project, and if the deploy fails, it classifies the failure, tries a fix, and
verifies the fix actually worked — install succeeded, every package actually imports, the app
process is running, and the health endpoint answers from the process it just started, not a stale
one still holding the port — before it trusts the fix and moves on.

It was built for AWS's WeMakeDevs **First Commit** hackathon, Ship It track.

## The core idea: cheap before expensive

The interesting design decision isn't "call an LLM when something breaks." It's the order NoMeshOps
tries things in, and the fact that it verifies every single one before believing it:

1. **A deterministic rules table.** Runs in well under a millisecond, touches no network. Most
   deploy failures are things that have been seen a thousand times before — a missing `git`
   binary, a source build that needs `libpq-dev`, a Python venv module that isn't installed on a
   minimal image. These get fixed instantly, for free.
2. **A DynamoDB knowledge base**, keyed by an error signature — a hash of the error type, the
   package, the OS, the architecture, and the Python version. The same error text on Python 3.9 and
   3.12 is treated as a different problem on purpose, because it often needs a different fix. A
   family-index GSI lets a fix discovered on one platform get a second chance on another, but only
   through the same verification gate — nothing is trusted just because it worked somewhere else
   once.
3. **Amazon Bedrock (Claude, Converse API)** — only when both of the above miss. One JSON object
   per call, under a strict contract, checked against a destructive-command deny-list before
   anything reaches the target. Capped at one model call per run. This is the expensive, slow,
   last-resort path — which is exactly how it should be used.

That ordering matters for a reason beyond elegance: Bedrock is the one piece of this stack with no
free tier at all — every call is billed from the first token. Treating it as a last resort isn't
just good architecture, it's the only way the economics of "fix deployments automatically" work at
scale.

## Architecture

```
Terminal / CLI                Browser (hosted demo, aws-login-style device sign-in)
      \                             /
       \                           /
      FastAPI + LangGraph orchestrator   (ECS Express Mode — one API call, load-balanced HTTPS)
              |
              |-- SSM Run Command -----> target EC2 instances  (fingerprint, deploy, verify)
              |                          no SSH keys, no inbound ports, full command audit trail
              |
              |-- boto3 ---------------> DynamoDB `deployment_fixes`  (signature -> verified fix)
              |-- boto3 ---------------> Amazon Bedrock (Claude)      (knowledge-base miss only)
              |-- boto3 ---------------> S3                           (full attempt audit log)
```

The orchestrator itself is a LangGraph state machine: `scan → fingerprint → deterministic_check →
deploy → verify → finalize`, with the rules → knowledge-base → Bedrock ladder branching off a
failed deploy and looping back to retry, capped at 3 fix attempts and 1 model call per run — then a
structured failure report with the full attempt history, never an infinite loop.

## How AWS is used

- **ECS Express Mode** hosts the FastAPI + LangGraph server. One `create-express-gateway-service`
  call gives a load-balanced public HTTPS URL — no manually wired ALB, target group, or security
  group.
- **AWS Systems Manager Run Command** is how every single interaction with a target machine
  happens: fingerprinting it, deploying to it, verifying it. No SSH keys, no open inbound ports,
  and every command is logged.
- **Amazon DynamoDB** is the knowledge base described above — on-demand billing, sub-second exact
  lookups, no vector search needed because the whole point is a verified, exact match.
- **Amazon Bedrock** generates a fix only on a genuine knowledge-base miss, one call per run, with
  fix commands checked against a deny-list before they ever reach a target.
- **Amazon S3** holds the audit trail: one JSON record per run with the fingerprint, the scan, every
  attempt's raw output, the fix chain and its source, and the full event timeline.
- **ECR, IAM, and CloudWatch Logs** round it out — the container registry, least-privilege roles
  scoped to exactly what each piece needs (the task role can only touch tagged instances, one
  table, one bucket), and container logs.

## The bugs that taught me the most

The most useful part of building this wasn't the happy path — it was finding real bugs by testing
against real infrastructure instead of trusting the design on paper.

**The demo's target picker was pointing at Docker container names.** The six-cloud target list
worked perfectly in local testing, where "instance id" is just a Docker container name. The moment
I deployed to real AWS with `EXECUTOR=ssm`, four of those six targets would have failed instantly
with an SSM `InvalidInstanceId` error the second someone clicked them — because AWS Systems Manager
needs an actual EC2 instance id, not a container name. Caught it by actually redeploying and
checking, not by re-reading the code.

**The `aws login`-style terminal sign-in had a real identity bug.** The device-approval flow and
the real email/password account system were two completely disconnected token stores. The frontend
was hardcoding the approving identity as a literal string, and the resulting session token lived in
a dictionary that the rest of the app's auth check never even looked at — meaning a real `nomeshops
login` from the terminal would silently not have worked against anything. Found this while wiring
up the real login flow end to end, not in a code review.

**Alpine Linux broke three assumptions at once.** No `bash` by default (the fix-runner assumed
one). Python packages are named `py3-pip`, not `python3-pip`. And `command -v apt-get dnf yum apk`
— checking several package managers in one call — silently returns nothing under busybox, because
busybox's `command` doesn't support multiple names in one invocation. All three only showed up when
I actually added a sixth, less common target instead of stopping at the familiar ones.

Every one of these is now covered by an automated test that reproduces the exact failure, and the
fixes are verified against real infrastructure, not just mocked.

## Results

Measured on real AWS (not simulated): a Bedrock-assisted fix completes in ~42s, a knowledge-base
hit with zero model calls in ~34s, and a purely deterministic fix in ~58s. A full run through ECS
Express Mode — clone, install, verify, and a knowledge-base hit — completed in 29.6s with a 45ms
lookup. The same error signature transfers correctly across a real Debian→Azure platform switch
with zero additional model calls, because the fix was already verified once.

## Try it

- **Code:** [github.com/SibgathKhan777/NoMeshOps](https://github.com/SibgathKhan777/NoMeshOps)
- **Live demo:** [no-7cbc2123abe543dfae0a7c09f665e2ff.ecs.ap-south-1.on.aws/demo](https://no-7cbc2123abe543dfae0a7c09f665e2ff.ecs.ap-south-1.on.aws/demo)
  — sign in and watch a real deploy fail, get diagnosed, and get fixed, streamed live over SSE,
  against real EC2 instances reachable over SSM. No fixture, no scripted outcome.

NoMeshOps doesn't eliminate the need to understand your infrastructure. It eliminates the tedious,
repetitive part of debugging the same class of failure over and over — and it only ever claims a
fix worked after actually proving it did.
