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

## The demo is real
`GET /api/demo/deploy?scenario=<typo|psycopg2|clean|crash>&target=<cloud-1|cloud-2>` runs `stream_deploy` against a
real target (`cloud-1` = `nomeshops-ubuntu22`, `cloud-2` = `nomeshops-al2023`) and streams `log`/`result` SSE events.
Nothing is faked. The scenarios map to branches of the sample repo. The per-target lock means two deploys to the same
target at once fail cleanly with `TargetBusy` — that is correct behaviour, shown live.

## Device auth endpoints (used by `nomeshops login`)
- `POST /api/device/start` → `{user_code, device_code}` (CLI)
- `GET  /api/device/lookup?user_code=` → confirms a typed code (browser)
- `POST /api/device/decision` `{user_code, approve}` (browser)
- `GET  /api/device/poll?device_code=` → `{status, session_token}` when approved (CLI)
- `GET  /api/session` with `Authorization: Bearer <token>` → session info

The store is in-memory (fine for a demo / single node). For production, back it with DynamoDB or Redis and put the
server behind HTTPS; codes expire in 10 min, sessions in 12 h.
