# NoMeshOps web platform

The orchestrator server (`app/main.py`) also serves a small web platform, so NoMeshOps is a hosted tool with a
browser front end, not only a CLI.

## Run it
```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```
Then open http://127.0.0.1:8090

## Pages
| path | what |
|---|---|
| `/` | landing page; shows live server status from `/health` |
| `/demo` | two-machine live demo — runs a REAL deploy against a target and streams every event over SSE |
| `/device` | `aws login`-style device sign-in: enter the code the CLI printed, approve, session returns to the shell |

## The demo deploys whatever repo you give it — it does not know the answer in advance

`GET /api/demo/targets` lists the six real targets. `GET /api/demo/deploy?target=<id>&repo=<url>[&branch=][&health_path=][&start_command=]`
runs `stream_deploy` against whichever machine you pick, against whichever public repo you give it, and streams
`log`/`result` SSE events. Earlier this always deployed one fixed sample repo and made the caller pick a "scenario"
naming which known failure to reproduce — that never matched what the product actually does (scan → fingerprint →
deploy → diagnose → fix, with no prior knowledge of what's wrong) and the UI contradicted its own "not replayed"
claim. Fixed 2026-09-19: `repo` is deployed as given, `branch`/`health_path`/`start_command` are optional overrides
(default: no override, so `start_command` is `None` and the agent auto-detects it from the scan exactly like the
CLI does — it was previously hardcoded to `uvicorn app:app`, which was itself a smaller version of the same bug).
`GET /api/demo/examples` lists four quick-fill example branches of the sample repo for a visitor with no broken
project of their own; the backend gives them no special treatment once resolved to a URL. `repo` is checked against
`ALLOWED_GIT_HOSTS` (github.com, gitlab.com, bitbucket.org, codeberg.org, git.sr.ht; https only) before anything
touches `git clone` on a shared target, closing off a crafted URL reaching something internal. Proved for real: a
throwaway public repo (`nomeshops-unseen-demo`) that the backend has never seen — no scenario, no branch, no
start command — was scanned, correctly flagged an `lxml` source-build issue from the fingerprint alone, fixed it,
auto-detected the FastAPI start command, and verified a real health response, all through the actual browser UI.
The per-target lock means two deploys to the same target at once fail cleanly with `TargetBusy` — that is correct
behaviour, shown live.

| id | cloud | image | package manager | default python |
|---|---|---|---|---|
| `aws-ubuntu` | AWS EC2 | `ubuntu:22.04` | apt-get | 3.10 |
| `aws-al2023` | AWS EC2 | `amazonlinux:2023` | dnf | 3.9 |
| `gcp-debian` | Google Cloud | `debian:12` | apt-get | 3.11 |
| `azure-ubuntu` | Azure / DigitalOcean | `ubuntu:24.04` | apt-get | 3.12 |
| `rocky` | Oracle Cloud / on-prem | `rockylinux:9` | dnf | 3.9 |
| `alpine` | Fly.io / lightweight VPS | `alpine:3.20` | apk | 3.12 |

Adding Alpine surfaced three real portability bugs, all fixed: `fingerprint.py`'s package-manager detection used
`command -v apt-get dnf yum apk` in one call, which busybox's `command` does not support with multiple names (it
silently returned nothing); `deploy.py` ran fix scripts with `bash`, which Alpine does not ship by default (switched
to `sh`, which is all the fix scripts ever needed); and `rules.py` assumed `python3-pip`/`python3-venv` package names
everywhere except apt-get, but Alpine's Python packages are named `py3-*` and `python3` already bundles `venv` (added
a real per-manager bootstrap table, `PYTHON_BOOTSTRAP`/`PIP_VENV_BOOTSTRAP`, instead of an apt-get-vs-everyone-else
guess). All six targets are verified end to end with real deploys, including the psycopg2 source-build and the
Debian→Azure knowledge-family transfer (0 model calls on the second cloud).

## The terminal login is real, end to end (fixed and verified 2026-09-20)

`nomeshops login --url <server>` (`cli/demo.py`) does the actual `aws login`-style exchange: `POST /api/device/start`
for a code, opens the browser at `<server>/device?code=XXXX-XXXX` (the page auto-fills and looks up that code),
polls `GET /api/device/poll` until approved, and saves `{token, email}` to `~/.nomeshops/session.json` (0600
permissions), keyed by server URL so multiple hosted servers can be signed into at once. `nomeshops whoami` and
`nomeshops logout` read/clear that file. `nomeshops deploy --url <server> ...` sends the saved token as a bearer
header to the new authenticated `POST /api/deploy/stream`; a 401 tells the user to log in again rather than failing
silently.

Earlier this session, the device-approval flow and the real email/password account system were two unconnected
token stores: `/api/device/decision` accepted a client-supplied `subject` string with **no verification at all**
(the frontend literally hardcoded `subject: 'sibgath'`), and the token it handed back lived in a separate dict that
`/api/deploy`'s auth check didn't even recognize — so a `nomeshops login` token would not have worked against
anything. Fixed: approval now requires the *browser itself* to be signed in with a real account
(`_account_from_request` on the decision call), and the CLI receives an exact copy of that account's own bearer
token from `_account_sessions`, the same store `/api/auth/me` and the demo quota use. The dead parallel `_sessions`
dict, `SESSION_TTL` constant, and the unused `/api/session` endpoint were removed rather than patched.

Verified for real, not just unit-tested: ran `nomeshops login --url http://127.0.0.1:8090` from a terminal, which
opened a real browser to `/device?code=...`; completed the sign-up and approval in that actual browser (not a
scripted one); confirmed `~/.nomeshops/session.json` held a working token; ran `nomeshops whoami` (correct email
and quota) and `nomeshops deploy --url ... --repo ... --instance nomeshops-ubuntu22`, which authenticated with the
saved token and streamed a real deploy of the same never-registered `nomeshops-unseen-demo` repo through
`/api/deploy/stream`, bootstrapping python/git/lxml build deps from a bare container and verifying a real health
response; then `nomeshops logout` followed by `whoami` correctly reported signed-out.

Endpoints:
- `POST /api/device/start` → `{user_code, device_code, verification_uri, expires_in, interval}` (CLI)
- `GET  /api/device/lookup?user_code=` → confirms a pending code and lists requested scopes (browser)
- `POST /api/device/decision` `{user_code, approve}`, `Authorization: Bearer <account token>` required to approve (browser)
- `GET  /api/device/poll?device_code=` → `{status, session_token, subject}` once approved (CLI)
- `POST /api/deploy` / `POST /api/deploy/stream` — authenticated equivalents of the plain `/deploy` and
  `/deploy/stream` on `app/main.py`; arbitrary `instance_id`, repo checked against `ALLOWED_GIT_HOSTS`

**Known gap, disclosed rather than hidden:** signing in proves who you are, not which target machines you may
reach. Any signed-in account can currently deploy to any `instance_id` this server's own AWS/Docker credentials can
reach — there is no per-account target ownership or registration model yet. The consent screen says so plainly
rather than claiming a restriction that does not exist.

The account/device store is in-memory plus a local JSON file (fine for a demo / single node). For production, back
it with DynamoDB or Redis and put the server behind HTTPS; device codes expire in 10 minutes, account sessions in
7 days.
