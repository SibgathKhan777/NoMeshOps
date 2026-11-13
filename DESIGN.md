# NoMeshOps — Design

This document describes the HTTP API, the deploy pipeline behind it, the data it stores, and the
design decisions (and known gaps) that shape it. For setup and usage, see [README.md](README.md);
for the hosted web platform, see [WEB.md](WEB.md).

## 1. System overview

NoMeshOps is a FastAPI service that deploys a Python project from a git repo onto a target machine
and, when the deploy fails, repairs it. Repair is tiered so that the cheapest source that can
answer is always asked first:

```
deterministic rules table  →  knowledge base of verified fixes  →  LLM (Bedrock), at most once
```

A fix only "counts" after it passes real verification on the target (install → import → app start
→ HTTP health check). Only then is it written to the knowledge base, so the next identical failure
resolves without an LLM call.

| Component | Responsibility | Production backend | Local backend |
|---|---|---|---|
| API (`app/main.py`, `app/web.py`) | HTTP + SSE surface, auth, demo quotas | ECS Express Mode | `uvicorn` |
| Orchestrator (`app/graph.py`) | LangGraph state machine for one deploy run | — | — |
| Executor (`app/aws/ssm.py`, `app/local/docker_exec.py`) | Runs shell scripts on the target | SSM Run Command | `docker exec` |
| Knowledge base (`app/nodes/knowledge.py`) | Verified fixes keyed by error signature | DynamoDB | JSON file |
| Audit store (`app/nodes/store.py`) | Full record of every run | S3 | local files |
| LLM (`app/nodes/bedrock_fix.py`) | Proposes a fix for a never-seen failure | Bedrock (Converse API) | Anthropic API / disabled |

Backends are chosen by environment variables (`EXECUTOR`, `KB_BACKEND`, `STORE_BACKEND`,
`LLM_BACKEND`), so the same code runs on AWS or fully offline.

## 2. API surfaces

There are two deliberately separate surfaces:

- **Self-hosted surface** (`app/main.py`): unauthenticated. Meant for someone running the
  orchestrator inside their own account against their own machines.
- **Hosted surface** (`app/web.py`, all under `/api`): requires a signed-in account. This is what
  the CLI (`nomeshops login` / `deploy`) and the browser use.

### 2.1 Self-hosted endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, plus region, fixes table, logs bucket and model id |
| `POST` | `/deploy` | Run one deploy synchronously; returns `DeployResponse` |
| `POST` | `/deploy/stream` | Same run, streamed as Server-Sent Events |
| `GET` | `/fingerprint/{instance_id}` | Fingerprint a target (OS, arch, Python, pip, venv, git, gcc, package manager) |
| `POST` | `/check` | Offline rule check: `{scan, fingerprint}` → predicted issues. No AWS calls |
| `GET` | `/fixes?limit=50` | List knowledge-base entries |
| `POST` | `/fixes/seed` | Insert a fix manually (`SeedFixRequest`) |
| `DELETE` | `/fixes/{error_signature}` | Remove a fix |
| `GET` | `/attempts?limit=20` | List audit records in S3 (`400` if `LOGS_BUCKET` is unset) |
| `GET` | `/`, `/demo`, `/device` | Static pages (landing, live demo, device sign-in) |

### 2.2 Hosted endpoints

**Accounts** (email + password)

| Method | Path | Body / params | Response | Errors |
|---|---|---|---|---|
| `POST` | `/api/auth/signup` | `{email, password}` | `{token, email, demo_runs_used, demo_runs_limit, demo_runs_remaining}` | `400` bad email / password < 8 chars, `409` already exists |
| `POST` | `/api/auth/login` | `{email, password}` | same as signup | `401` wrong email or password |
| `GET` | `/api/auth/me` | bearer token | account summary | `401` not signed in |
| `POST` | `/api/auth/logout` | bearer token | `{ok: true}` | — |

**Device-code sign-in** (how the CLI logs in, modelled on `aws login`)

| Method | Path | Called by | Purpose |
|---|---|---|---|
| `POST` | `/api/device/start` | CLI | Returns `user_code` (e.g. `BCDF-2345`), `device_code`, `verification_uri`, `expires_in` (600 s), `interval` (2 s) |
| `GET` | `/api/device/lookup?user_code=` | Browser | Confirms the code exists before showing consent; returns requested scopes. `404` if unknown/expired |
| `POST` | `/api/device/decision` | Browser | `{user_code, approve}`. Approving requires the browser to be signed in (`401` otherwise) |
| `GET` | `/api/device/poll?device_code=` | CLI | `pending` / `denied` / `expired`, or `approved` + `session_token` |

```
CLI                          Server                         Browser (signed in)
 │ POST /api/device/start ──▶│                                │
 │◀── user_code, device_code │                                │
 │   (shows code + URL)      │◀── GET /api/device/lookup ─────│ user types code
 │                           │◀── POST /api/device/decision ──│ user approves
 │ GET /api/device/poll ────▶│                                │
 │◀── approved + token       │                                │
```

**Deploys and demo**

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/deploy` | bearer | Synchronous deploy of any allowed repo to any target the server can reach |
| `POST` | `/api/deploy/stream` | bearer | Same, as SSE |
| `GET` | `/api/demo/targets` | none | Targets the server can actually reach right now (UI picker reads this) |
| `GET` | `/api/demo/examples` | none | Quick-fill example repos/branches for the demo |
| `GET` | `/api/demo/deploy?target=&repo=&branch=&health_path=&start_command=` | token (header or `?token=`) | Real deploy on a demo target, streamed as SSE. Limited to 3 runs per account |
| `GET` | `/api/fixes` | none | Up to 100 knowledge-base entries for the dashboard |

Auth tokens are accepted either as `Authorization: Bearer <token>` or as a `token` query
parameter, because the browser `EventSource` API cannot set custom headers.

### 2.3 Request and response models

`DeployRequest` (`app/models.py`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `repo_url` | string | required | Git URL of the project |
| `instance_id` | string | required | SSM-managed EC2 id, or a container name in local mode |
| `branch` | string? | repo default | Branch or tag |
| `start_command` | string? | auto-detected | Auto-detected for FastAPI/Flask if omitted |
| `app_port` | int | `8000` | Port polled by the health check |
| `health_path` | string | `/health` | Path polled by the health check |
| `keep_running` | bool | `false` | Leave the app running after a verified deploy |
| `preempt_predicted_fixes` | bool? | env setting | Apply rule-predicted fixes before the first attempt |

`DeployResponse`: `run_id`, `final_status`, `deploy_success`, `failure_reason`, `fingerprint`,
`scan`, `predicted_issues[]`, `attempts[]`, `fix_applied`, `stored_fix`, `s3_key`, `events[]`,
`duration_s`.

### 2.4 Streaming format

All `/stream` and demo endpoints use Server-Sent Events:

```
event: meta     (demo only) target, label, repo, branch, runs used / limit
event: log      one per orchestrator event: {level, node, t, msg, ...}
event: result   final: {final_status, deploy_success, failure_reason, stored_fix, duration_s, s3_key?}
```

The `result` event is always sent, even if the run raised an exception, so clients never hang.

## 3. Deploy pipeline (LangGraph)

```
scan_project → fingerprint_target → deterministic_check → deploy → verify → finalize
                                                            ▲   │      │
                                                            │   └──────┴── failure → route_failure
                                                            │                       │
                                         lookup_rules ──────┤◀──────────────────────┤
                                         lookup_knowledge ──┤◀──────────────────────┤
                                         ask_bedrock ───────┘◀──────────────────────┤
                                         retry_transient ──▶ deploy                 └──▶ finalize
```

| Node | What it does |
|---|---|
| `scan_project` | Shallow clone; parse `requirements.txt`, `pyproject.toml`, `Dockerfile` |
| `fingerprint_target` | One script on the target → OS, arch, Python, pip, venv, git, gcc, package manager |
| `deterministic_check` | Predict known incompatibilities before deploying; optionally pre-apply their fixes |
| `deploy` | Take the per-target lock, clean clone, create venv, apply fix stages, `pip install` |
| `verify` | Import check → start the app → poll the HTTP health path |
| `lookup_rules` | Error pattern → fix table. No I/O |
| `lookup_knowledge` | Exact signature match first, then the error's family index |
| `ask_bedrock` | One LLM call proposing a fix command |
| `retry_transient` | Plain retry for network/infra hiccups |
| `finalize` | Release the lock, write the audit record, store the fix if it was verified |

### 3.1 Failure routing (`route_failure`)

Checked in this order:

1. No error, or `MAX_FIX_ATTEMPTS` (3) reached → `finalize`.
2. `GitError` (bad URL, branch or credentials) → `finalize`. No fix can help.
3. `TargetBusy`, `UnreachableTarget`, `PortConflict` → `finalize`. These are infrastructure
   problems, so no fix attempt or LLM budget is spent on them.
4. `TransientError` → `retry_transient` once (`MAX_TRANSIENT_RETRIES`), then `finalize`.
5. Otherwise try, per error signature, `rules` → `knowledge` → `bedrock` (Bedrock only while
   `llm_attempts < MAX_LLM_ATTEMPTS`, default 1).

### 3.2 Error classification

`app/nodes/signature.py` turns raw output into a structured error with two hashes:

```
signature = hash(error_type, package, package_version, os_key, arch, python_minor, normalized_message)
family    = hash(error_type, package, normalized_message)          # ignores the runtime context
```

The `signature` gives an exact match for the same failure on the same kind of machine. The
`family` is a fallback that lets a fix learned on Ubuntu be tried on Amazon Linux, but it is still
only accepted after verification passes. Error types include `BuildError`, `PipError`,
`ResolutionError`, `ImportError`, `SyntaxError`, `StartError`, `HealthCheckError`,
`RuntimeMissing`, `PermissionError`, `GitError`, `PortConflict`, `UnreachableTarget` and
`UnknownError`.

## 4. Data model

### 4.1 Knowledge base — DynamoDB table `deployment_fixes`

| Attribute | Notes |
|---|---|
| `error_signature` | **Partition key** |
| `error_family` | Partition key of GSI `family-index` |
| `error_type`, `package`, `package_version`, `normalized_error` | From classification |
| `os`, `os_pretty`, `architecture`, `python_version` | From the fingerprint |
| `fix_command`, `description`, `source` | `source` is `rules`, `knowledge`, `bedrock` or `seed` |
| `success_count`, `failure_count` | Family lookups rank by `success − failure` |
| `first_seen_at`, `last_verified_at` | ISO-8601 UTC |
| `superseded_fix`, `superseded_at` | Set when a different fix replaces an existing one |

Re-learning never erases history: `first_seen_at` and `failure_count` are kept, and a replaced fix
is recorded in `superseded_fix`.

### 4.2 Audit trail — S3

Every run writes its full record to `s3://$LOGS_BUCKET/attempts/<UTC-timestamp>-<instance_id>-<run_id>.json`.

### 4.3 Accounts and sessions

Users are stored in `<LOCAL_DATA_DIR>/users.json` (written atomically through a temp file and
rename). Passwords are hashed with PBKDF2-SHA256, 200,000 iterations, with a random salt per user.
Session tokens are random 32-byte URL-safe strings that are valid for 7 days.

## 5. Design decisions

- **The LLM is the last resort, not the first.** Most deploy failures are repeats, so a rules table
  and a verified-fix cache answer them for free and deterministically. Capping LLM use at one call
  per run keeps cost and latency predictable.
- **Verify before you remember.** A fix is stored only after a real install, import, start and
  health check. Transient network errors are never learned, because crediting whatever fix
  happened to run before a recovery would poison the knowledge base.
- **SSM instead of SSH.** Targets need no inbound ports and no SSH keys, and access is governed by
  IAM.
- **One token namespace.** Device-code approval hands the CLI a copy of the signed-in browser's own
  token, tied to a verified identity, instead of trusting a client-supplied name.
- **Validate before charging quota.** On the demo endpoint the target and repo URL are validated
  before a free run is deducted, so a typo does not cost the user a run.
- **Locked-down clone URLs.** Only `https://` URLs on `github.com`, `gitlab.com`, `bitbucket.org`,
  `codeberg.org` and `git.sr.ht` are accepted. This keeps a crafted URL from making `git clone`
  reach internal addresses or metadata endpoints.
- **One deploy per target at a time.** A per-target lock rejects a concurrent deploy (`TargetBusy`)
  instead of letting two runs corrupt the same machine.
- **Pluggable backends.** Every AWS dependency has a local stand-in, so the full pipeline can be
  tested offline with Docker targets.

## 6. Known gaps

- **No per-account ownership of targets.** Signing in proves who the user is, not which machines
  they may deploy to. Any signed-in account can deploy to any `instance_id` the server's own
  credentials can reach. This must be fixed before real multi-tenant use.
- **In-memory sessions and device grants.** They are lost on restart and are not shared between
  replicas. Running more than one replica would need a shared store (for example DynamoDB or
  Redis).
- **File-based user store.** `users.json` behind a process lock suits a single instance only.
- **The self-hosted surface has no auth** by design. It must not be exposed publicly.
