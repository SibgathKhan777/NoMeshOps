"""Web platform: device-code sign-in (like `aws login`), a dashboard, and a live demo that runs a
real deploy against the configured targets and streams every event over SSE. Mounted by app.main."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app.config import settings
from app.graph import run_deploy, stream_deploy, summarize
from app.models import DeployRequest
from app.nodes import knowledge

router = APIRouter()

# --------------------------------------------------------------------------- accounts (email + password)
DEMO_RUN_LIMIT = 3
ACCOUNT_SESSION_TTL = 7 * 24 * 3600  # 7 days
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_users_lock = threading.Lock()


def _users_path() -> str:
    os.makedirs(settings.local_data_dir, exist_ok=True)
    return os.path.join(settings.local_data_dir, "users.json")


def _load_users() -> dict[str, dict]:
    try:
        with open(_users_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(users: dict[str, dict]) -> None:
    tmp = _users_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, _users_path())


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_password(password, salt), stored)


_account_sessions: dict[str, dict] = {}  # token -> {email, created}


def _account_from_request(request: Request) -> dict | None:
    """Accepts the token as a bearer header (fetch/XHR) or a `token` query param
    (EventSource cannot set custom headers), so both call styles work."""
    tok = request.headers.get("authorization", "")
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    if not tok:
        tok = request.query_params.get("token", "")
    sess = _account_sessions.get(tok)
    if not sess or time.time() - sess["created"] > ACCOUNT_SESSION_TTL:
        return None
    users = _load_users()
    user = users.get(sess["email"])
    if not user:
        return None
    return {"email": sess["email"], "token": tok, "demo_runs": user.get("demo_runs", 0)}


def _account_public(email: str, users: dict[str, dict] | None = None) -> dict:
    users = users if users is not None else _load_users()
    u = users.get(email, {})
    used = int(u.get("demo_runs", 0))
    return {"email": email, "demo_runs_used": used, "demo_runs_limit": DEMO_RUN_LIMIT,
            "demo_runs_remaining": max(0, DEMO_RUN_LIMIT - used)}


@router.post("/api/auth/signup")
def auth_signup(payload: dict):
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "enter a valid email address")
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    with _users_lock:
        users = _load_users()
        if email in users:
            raise HTTPException(409, "an account with this email already exists — sign in instead")
        users[email] = {"password": _hash_password(password), "demo_runs": 0, "created_at": time.time()}
        _save_users(users)
    token = secrets.token_urlsafe(32)
    _account_sessions[token] = {"email": email, "created": time.time()}
    return {"token": token, **_account_public(email, users)}


@router.post("/api/auth/login")
def auth_login(payload: dict):
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    users = _load_users()
    user = users.get(email)
    if not user or not _verify_password(password, user.get("password", "")):
        raise HTTPException(401, "wrong email or password")
    token = secrets.token_urlsafe(32)
    _account_sessions[token] = {"email": email, "created": time.time()}
    return {"token": token, **_account_public(email, users)}


@router.get("/api/auth/me")
def auth_me(request: Request):
    acc = _account_from_request(request)
    if not acc:
        raise HTTPException(401, "not signed in")
    return _account_public(acc["email"])


@router.post("/api/auth/logout")
def auth_logout(request: Request):
    tok = request.headers.get("authorization", "")
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    _account_sessions.pop(tok, None)
    return {"ok": True}

# --------------------------------------------------------------------------- device-code auth
# Deliberately reuses the SAME token namespace as email+password login (_account_sessions), not a
# parallel one: approving a device request just hands the CLI a copy of the browser's own already-
# valid account token, tied to the real signed-in identity. An earlier version of this trusted a
# client-supplied "subject" string with no verification at all — fixed here, not just wired up.
CODE_TTL = 600  # seconds a device code is valid before approval


@dataclass
class DeviceGrant:
    user_code: str
    device_code: str
    created: float = field(default_factory=time.time)
    status: str = "pending"        # pending | approved | denied | expired
    session_token: str | None = None
    subject: str | None = None     # the approving account's real email, set from its verified session

    def expired(self) -> bool:
        return time.time() - self.created > CODE_TTL


_grants: dict[str, DeviceGrant] = {}         # user_code -> grant
_by_device: dict[str, DeviceGrant] = {}      # device_code -> grant
_ALPHA = "BCDFGHJKLMNPQRSTVWXZ23456789"


def _user_code() -> str:
    raw = "".join(secrets.choice(_ALPHA) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def _sweep() -> None:
    for uc, g in list(_grants.items()):
        if g.status == "pending" and g.expired():
            g.status = "expired"


@router.post("/api/device/start")
def device_start(payload: dict | None = None):
    """The CLI calls this to begin a login. Returns a user_code to display and a device_code to poll."""
    _sweep()
    uc, dc = _user_code(), secrets.token_urlsafe(24)
    g = DeviceGrant(user_code=uc, device_code=dc)
    _grants[uc] = g
    _by_device[dc] = g
    return {
        "user_code": uc,
        "device_code": dc,
        "verification_uri": "/device",
        "expires_in": CODE_TTL,
        "interval": 2,
        "session_ttl": ACCOUNT_SESSION_TTL,
    }


@router.get("/api/device/lookup")
def device_lookup(user_code: str):
    """The browser confirms a typed code is real before showing the consent screen."""
    _sweep()
    g = _grants.get(user_code.upper().strip())
    if not g or g.status == "expired":
        raise HTTPException(404, "unknown or expired code")
    return {"user_code": g.user_code, "status": g.status, "requested_scopes": ["deploy", "fixes:read", "fixes:write", "logs:write"]}


@router.post("/api/device/decision")
def device_decision(payload: dict, request: Request):
    """The browser approves or denies after the user consents. Approval requires the browser
    itself to be signed in with a real account — the CLI ends up with an exact copy of that
    account's own token, never an invented identity."""
    _sweep()
    g = _grants.get(str(payload.get("user_code", "")).upper().strip())
    if not g:
        raise HTTPException(404, "unknown code")
    if g.status != "pending":
        return {"status": g.status}
    if payload.get("approve"):
        acc = _account_from_request(request)
        if not acc:
            raise HTTPException(401, "sign in with your NoMeshOps account first")
        g.status = "approved"
        g.session_token = acc["token"]
        g.subject = acc["email"]
    else:
        g.status = "denied"
    return {"status": g.status}


@router.get("/api/device/poll")
def device_poll(device_code: str):
    """The CLI polls until the user approves in the browser, then receives the session token."""
    _sweep()
    g = _by_device.get(device_code)
    if not g:
        raise HTTPException(404, "unknown device code")
    if g.status == "approved":
        return {"status": "approved", "session_token": g.session_token, "subject": g.subject, "expires_in": ACCOUNT_SESSION_TTL}
    return {"status": g.status}


# NOTE: there used to be a second, parallel /api/session + _auth() here backed by a session store
# that no longer exists (see the note on the device-code section above). GET /api/auth/me is the
# one real equivalent now — both the CLI's `whoami` and the browser use it.


# --------------------------------------------------------------------------- live demo (real deploy)
# Six target machines: the default OS image of five popular clouds/hosts, spanning three
# package managers, so the demo proves the agent on more than just AWS. Each entry is a real
# Docker container (scripts/local_targets.sh) or, in AWS mode, a real EC2 instance id.
DEMO_TARGETS = {
    "aws-ubuntu":   {"instance": os.getenv("DEMO_INSTANCE_AWS_UBUNTU", "nomeshops-ubuntu22"),
                     "cloud": "AWS EC2", "label": "AWS EC2 · Ubuntu 22.04 · x86_64", "port": 8000},
    "aws-al2023":   {"instance": os.getenv("DEMO_INSTANCE_AWS_AL2023", "nomeshops-al2023"),
                     "cloud": "AWS EC2", "label": "AWS EC2 · Amazon Linux 2023 · x86_64", "port": 8001},
    "gcp-debian":   {"instance": os.getenv("DEMO_INSTANCE_GCP_DEBIAN", "nomeshops-gcp-debian"),
                     "cloud": "Google Cloud", "label": "Google Cloud · Debian 12 · x86_64", "port": 8002},
    "azure-ubuntu": {"instance": os.getenv("DEMO_INSTANCE_AZURE_UBUNTU", "nomeshops-azure-ubuntu"),
                     "cloud": "Azure / DigitalOcean", "label": "Azure / DigitalOcean · Ubuntu 24.04 · x86_64", "port": 8003},
    "rocky":        {"instance": os.getenv("DEMO_INSTANCE_ROCKY", "nomeshops-rocky"),
                     "cloud": "Oracle Cloud / on-prem", "label": "Oracle Cloud / on-prem · Rocky Linux 9 · x86_64", "port": 8004},
    "alpine":       {"instance": os.getenv("DEMO_INSTANCE_ALPINE", "nomeshops-alpine"),
                     "cloud": "Fly.io / lightweight VPS", "label": "Fly.io / lightweight VPS · Alpine 3.20 · x86_64", "port": 8005},
}
DEMO_REPO = "https://github.com/SibgathKhan777/nomeshops-sample.git"


@router.get("/api/demo/targets")
def demo_targets():
    """Single source of truth for the target picker, so the UI can never drift from what the
    server can actually reach. In docker mode every container-name target is reachable by
    definition. In ssm mode, only a target explicitly pointed at a real EC2 instance id (i-...)
    via a DEMO_INSTANCE_* env var is listed — the rest would just be an SSM InvalidInstanceId
    error against a container name that has no EC2 counterpart on this deployment."""
    live = {
        k: v for k, v in DEMO_TARGETS.items()
        if settings.executor == "docker" or v["instance"].startswith("i-")
    }
    return {"targets": [{"id": k, **v, "port": v["port"]} for k, v in live.items()]}


# The demo deploys ANY public repo the visitor gives it — that is the actual product, not a
# fixture. These four are just quick-fill examples for someone without a broken repo handy;
# the backend does not treat them specially once past this URL-shaped fill-in.
DEMO_EXAMPLES = {
    "typo": {"branch": None, "description": "Misspelled dependency — the agent has never seen this exact error before, so it asks the model for one fix"},
    "psycopg2": {"branch": "eval-psycopg2", "description": "A database driver that compiles from source — the deterministic rules table predicts and fixes this before the first attempt"},
    "clean": {"branch": "eval-clean", "description": "A healthy project — nothing to fix"},
    "crash": {"branch": "eval-start-crash", "description": "Install exits 0, but the app crashes at startup — the health check catches what the exit code missed"},
}

ALLOWED_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht"}


def _validate_repo_url(repo: str) -> str:
    """Only https:// URLs on known public git hosts. This runs on a shared machine, so this is
    not just tidiness: it keeps a crafted URL from making `git clone` reach something internal
    (a metadata endpoint, a private network address, a non-http scheme) instead of a real repo."""
    from urllib.parse import urlparse
    repo = (repo or "").strip()
    if not repo:
        raise HTTPException(400, "enter a git repository URL")
    if len(repo) > 300:
        raise HTTPException(400, "that URL is too long")
    u = urlparse(repo)
    if u.scheme != "https" or u.netloc.lower() not in ALLOWED_GIT_HOSTS or not u.path.strip("/"):
        raise HTTPException(400, f"the hosted demo only deploys public repos on {', '.join(sorted(ALLOWED_GIT_HOSTS))}")
    return repo


@router.get("/api/demo/deploy")
def demo_deploy(request: Request, target: str = "aws-ubuntu", repo: str = "", branch: str = "",
                health_path: str = "/health", start_command: str = ""):
    """Run a REAL deploy against a configured target and stream every orchestrator event as SSE.
    `repo` is deployed exactly as given — scanned, fingerprinted, and if it fails, classified and
    fixed the same way the CLI would, with no foreknowledge of what is wrong with it. The UI's
    example chips are just a convenience that fill `repo`/`branch` client-side before calling this;
    nothing here treats them specially.
    Gated: requires a signed-in account and enforces DEMO_RUN_LIMIT uses per account."""
    acc = _account_from_request(request)
    if not acc:
        raise HTTPException(401, "sign in to run the live demo")

    # Validate everything about the request itself before touching the account's run quota —
    # an unreachable target or a bad repo URL must not cost the account one of its 3 free runs.
    tgt = DEMO_TARGETS.get(target)
    if not tgt or not (settings.executor == "docker" or tgt["instance"].startswith("i-")):
        raise HTTPException(400, "unknown target")
    repo_url = _validate_repo_url(repo or DEMO_REPO)
    branch = branch.strip() or None

    with _users_lock:
        users = _load_users()
        user = users.get(acc["email"])
        if not user:
            raise HTTPException(401, "sign in to run the live demo")
        used = int(user.get("demo_runs", 0))
        if used >= DEMO_RUN_LIMIT:
            raise HTTPException(403, f"you've used all {DEMO_RUN_LIMIT} free demo runs on this account")
        user["demo_runs"] = used + 1
        _save_users(users)
    req = {
        "repo_url": repo_url, "instance_id": tgt["instance"], "branch": branch,
        "app_port": tgt["port"], "health_path": (health_path or "/health").strip(),
        "keep_running": False, "preempt_predicted_fixes": True,
        "start_command": start_command.strip() or None,  # None -> the agent auto-detects from the scan, same as the CLI
    }

    def gen():
        yield f"event: meta\ndata: {json.dumps({'target': target, 'label': tgt['label'], 'repo': repo_url, 'branch': branch, 'demo_runs_used': used + 1, 'demo_runs_limit': DEMO_RUN_LIMIT})}\n\n"
        final = None
        try:
            for node, delta in stream_deploy(req):
                for ev in delta.get("events", []) or []:
                    yield f"event: log\ndata: {json.dumps(ev)}\n\n"
                if node == "finalize":
                    final = delta
        except Exception as e:  # noqa: BLE001
            yield f"event: log\ndata: {json.dumps({'level':'error','node':'server','t':0,'msg':f'{type(e).__name__}: {e}'})}\n\n"
        result = {
            "deploy_success": bool((final or {}).get("deploy_success")),
            "final_status": (final or {}).get("final_status", "error"),
            "failure_reason": (final or {}).get("failure_reason"),
            "stored_fix": bool((final or {}).get("stored_fix")),
            "duration_s": (final or {}).get("duration_s"),
        }
        yield f"event: result\ndata: {json.dumps(result)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/fixes")
def web_fixes():
    try:
        return {"items": knowledge.list_fixes(100)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"items": [], "error": f"{type(e).__name__}: {e}"})


@router.get("/api/demo/examples")
def demo_examples():
    """Quick-fill examples for the UI — not a menu of required choices."""
    return {"examples": [{"id": k, **v} for k, v in DEMO_EXAMPLES.items()]}


# --------------------------------------------------------------------------- authenticated deploy API
# The hosted, multi-tenant surface: what `nomeshops login` + `nomeshops deploy --url` actually talk to.
# Distinct from the plain /deploy and /deploy/stream on app/main.py, which are unauthenticated by
# design for someone self-hosting the orchestrator against their own account and own targets.
#
# Known gap, disclosed rather than hidden: signing in here proves who you are, not which target
# machines you are allowed to reach. Any signed-in account may currently deploy to any instance_id
# this server's own AWS/Docker credentials can reach — there is no per-account target ownership or
# registration model yet. That is a real limitation for multi-tenant production use, not just an
# unfinished feature.

@router.post("/api/deploy", response_model=None)
def api_deploy(req: DeployRequest, request: Request):
    if not _account_from_request(request):
        raise HTTPException(401, "sign in first — run `nomeshops login`")
    payload = req.model_dump()
    payload["repo_url"] = _validate_repo_url(payload["repo_url"])
    return summarize(run_deploy(payload))


@router.post("/api/deploy/stream")
def api_deploy_stream(req: DeployRequest, request: Request):
    """Same event shape as the unauthenticated /deploy/stream, gated behind a signed-in account."""
    if not _account_from_request(request):
        raise HTTPException(401, "sign in first — run `nomeshops login`")
    payload = req.model_dump()
    payload["repo_url"] = _validate_repo_url(payload["repo_url"])

    def gen():
        final = None
        for node, delta in stream_deploy(payload):
            for ev in delta.get("events", []) or []:
                yield f"event: log\ndata: {json.dumps(ev)}\n\n"
            if node == "finalize":
                final = delta
        result = {"final_status": (final or {}).get("final_status", "error"),
                  "deploy_success": bool((final or {}).get("deploy_success")),
                  "failure_reason": (final or {}).get("failure_reason"),
                  "s3_key": (final or {}).get("s3_key"), "stored_fix": bool((final or {}).get("stored_fix")),
                  "duration_s": (final or {}).get("duration_s")}
        yield f"event: result\ndata: {json.dumps(result)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
