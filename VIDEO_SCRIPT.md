# Submission video script (target: 2:55–3:00)

Read at a normal, unhurried pace (~150 wpm). Timestamps are targets, not hard cuts — let the live
demo breathe; don't rush the SSE log scrolling by.

---

## 0:00–0:35 — About the project

**[ON SCREEN: `/demo` landing page, live at https://no-7cbc2123abe543dfae0a7c09f665e2ff.ecs.ap-south-1.on.aws]**

> "This is NoMeshOps — a self-healing deployment agent. You give it a target machine and a Python
> repo, and it deploys the project. When the deploy fails — a missing dependency, a source build
> that needs a system library, an app that crashes on startup — it doesn't just report the error.
> It diagnoses it and fixes it itself, then verifies the fix is real before it trusts it.
>
> It's for developers and small teams who don't have a dedicated DevOps engineer babysitting every
> deploy across different operating systems, architectures, and Python versions. Let me show you."

**[ACTION: paste a broken repo URL into the demo, select AWS EC2 Ubuntu 22.04, click deploy — let it stream live for a few seconds]**

> "That's a real deploy against a real EC2 instance right now, not a recording."

---

## 0:35–1:15 — Tech stack and architecture

**[ON SCREEN: switch tab to github.com/SibgathKhan777/NoMeshOps README, scroll to the Architecture section — the Mermaid diagram]**

> "Under the hood: a FastAPI server, hosting a LangGraph orchestrator, deployed on ECS Express
> Mode. The orchestrator runs a fixed pipeline — scan the repo, fingerprint the target machine,
> deploy, verify — and when a deploy fails, it tries three things in order: a deterministic rules
> table first, since most failures are things we already know how to fix and that costs nothing;
> a DynamoDB knowledge base of every fix we've verified before, matched by an exact error
> signature; and only if both of those miss, one single call to Claude on Amazon Bedrock.
>
> Verification is the actual gate, not the exit code. A fix only counts once the package installs,
> every dependency actually imports, the app process starts, the health endpoint answers — and the
> process that answered is the one we just started, not a stale one still holding the port."

---

## 1:15–2:25 — How you used AWS

**[ON SCREEN: can stay on the architecture diagram, or cut back to the live demo log if it's still running]**

> "On the build side, the AWS SDK for Python — boto3 — is the backbone of every AWS integration
> here: SSM command execution, DynamoDB reads and writes, S3 uploads, the Bedrock Converse API.
> Nothing goes through a higher-level abstraction — the actual SSM commands and DynamoDB items are
> all visible and tested directly.
>
> On the ship side: ECS Express Mode hosts this whole server — one API call gave me a
> load-balanced HTTPS URL with no manually-wired load balancer or security groups. AWS Systems
> Manager Run Command does every interaction with a target machine — fingerprinting it, deploying
> to it, verifying it — with no SSH keys and no inbound ports open, ever. DynamoDB is the knowledge
> base: the partition key is a hash of the error type, the package, the OS, the architecture, and
> the Python version, so the same error on two different platforms is correctly treated as two
> different problems, with a family index that lets a fix travel across platforms once
> re-verified. Bedrock — Claude — only gets called on a genuine knowledge-base miss, capped at one
> call per run, and its output runs through a destructive-command filter before anything reaches
> SSM. S3 holds a full audit trail — every attempt, every fix, every verification result — and ECR,
> IAM, and CloudWatch round out the deployment: the container registry, least-privilege roles
> scoped to exactly what this needs, and the logs."

**[ACTION: if the earlier demo has now hit a real failure and fix, show the moment the fix applies and the health check turns green]**

> "And there it is — verified, in under a minute and a half, with zero manual intervention."

---

## 2:25–2:55 — Learning and growth (optional but worth keeping — real bugs, not polish)

**[ON SCREEN: can be talking-head, or the GitHub commit history scrolled to a recent fix commit]**

> "The most useful part of building this wasn't the happy path — it was the bugs I found testing
> it against real infrastructure instead of trusting it. The demo's target picker was pointing at
> Docker container names that only mean something in local testing mode — against real AWS, that
> would have failed instantly with an invalid-instance error the moment someone clicked it. And
> separately, the device sign-in flow — the `aws login`-style terminal auth — had a real identity
> bug: it was handing out a session token from a completely disconnected store that the rest of
> the app never checked, so a real login would silently not have worked anywhere. Both are fixed,
> tested, and verified against the real, running deployment — not just unit tests."

---

## 2:55–3:00 — Close

**[ON SCREEN: back to the live demo or the repo]**

> "NoMeshOps — self-healing deployments, verified for real, on AWS. Thanks for watching."

---

### Notes for recording
- Keep the demo tab pre-loaded and signed in before you hit record, so you don't burn time on login.
- Use the `aws-ubuntu` target for the live segment — it's the fastest of the two real targets.
- If the live deploy is slow that day, have a second browser tab ready with a completed run's SSE
  log to cut to, so the video doesn't stall waiting on a real network call.
- Total spoken word count is ~430 words — at 150 wpm that's ~2:50, leaving margin for the demo
  cuts and pauses. Trim the "Learning and growth" section first if you're running over.
