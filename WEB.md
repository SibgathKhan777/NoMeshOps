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

## The demo is real, across six target machines

`GET /api/demo/targets` lists the six real targets; `GET /api/demo/deploy?scenario=<typo|psycopg2|clean|crash>&target=<id>`
runs `stream_deploy` against whichever one you pick and streams `log`/`result` SSE events. Nothing is faked. The
scenarios map to branches of the sample repo. The per-target lock means two deploys to the same target at once fail
cleanly with `TargetBusy` — that is correct behaviour, shown live.

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

## Device auth endpoints (used by `nomeshops login`)
- `POST /api/device/start` → `{user_code, device_code}` (CLI)
- `GET  /api/device/lookup?user_code=` → confirms a typed code (browser)
- `POST /api/device/decision` `{user_code, approve}` (browser)
- `GET  /api/device/poll?device_code=` → `{status, session_token}` when approved (CLI)
- `GET  /api/session` with `Authorization: Bearer <token>` → session info

The store is in-memory (fine for a demo / single node). For production, back it with DynamoDB or Redis and put the
server behind HTTPS; codes expire in 10 min, sessions in 12 h.
