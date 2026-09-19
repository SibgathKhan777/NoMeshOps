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
from app.graph import stream_deploy
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
CODE_TTL = 600          # seconds a device code is valid before approval
SESSION_TTL = 12 * 3600  # granted session lifetime


@dataclass
class DeviceGrant:
    user_code: str
    device_code: str
    created: float = field(default_factory=time.time)
    status: str = "pending"        # pending | approved | denied | expired
    session_token: str | None = None
    subject: str | None = None

    def expired(self) -> bool:
        return time.time() - self.created > CODE_TTL


_grants: dict[str, DeviceGrant] = {}         # user_code -> grant
_by_device: dict[str, DeviceGrant] = {}      # device_code -> grant
_sessions: dict[str, dict] = {}              # session_token -> {subject, created}
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
def device_decision(payload: dict):
    """The browser approves or denies after the user consents."""
    _sweep()
    g = _grants.get(str(payload.get("user_code", "")).upper().strip())
    if not g:
        raise HTTPException(404, "unknown code")
    if g.status not in ("pending",):
        return {"status": g.status}
    if payload.get("approve"):
        g.status = "approved"
        g.session_token = secrets.token_urlsafe(32)
        g.subject = payload.get("subject") or "sibgath"
        _sessions[g.session_token] = {"subject": g.subject, "created": time.time()}
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
        return {"status": "approved", "session_token": g.session_token, "subject": g.subject, "expires_in": SESSION_TTL}
    return {"status": g.status}


def _auth(request: Request) -> dict | None:
    tok = request.headers.get("authorization", "")
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    sess = _sessions.get(tok)
    if sess and time.time() - sess["created"] < SESSION_TTL:
        return sess
    return None


@router.get("/api/session")
def session_info(request: Request):
    sess = _auth(request)
    if not sess:
        raise HTTPException(401, "no session")
    return {"subject": sess["subject"], "age_s": round(time.time() - sess["created"])}


# --------------------------------------------------------------------------- live demo (real deploy)
DEMO_TARGETS = {
    "cloud-1": {"instance": "nomeshops-ubuntu22", "label": "Ubuntu 22.04 · x86_64"},
    "cloud-2": {"instance": "nomeshops-al2023", "label": "Amazon Linux 2023 · x86_64"},
}
DEMO_REPO = "https://github.com/SibgathKhan777/nomeshops-sample.git"
DEMO_BRANCHES = {
    "typo": None,                    # default branch ships the reqests typo
    "psycopg2": "eval-psycopg2",
    "clean": "eval-clean",
    "crash": "eval-start-crash",
}
DEMO_PORTS = {"cloud-1": 8000, "cloud-2": 8001}


@router.get("/api/demo/deploy")
def demo_deploy(request: Request, scenario: str = "typo", target: str = "cloud-1"):
    """Run a REAL deploy against a configured target and stream every orchestrator event as SSE.
    Gated: requires a signed-in account and enforces DEMO_RUN_LIMIT uses per account."""
    acc = _account_from_request(request)
    if not acc:
        raise HTTPException(401, "sign in to run the live demo")
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
    tgt = DEMO_TARGETS.get(target)
    if not tgt:
        raise HTTPException(400, "unknown target")
    branch = DEMO_BRANCHES.get(scenario, None)
    req = {
        "repo_url": DEMO_REPO, "instance_id": tgt["instance"], "branch": branch,
        "app_port": DEMO_PORTS.get(target, 8000), "health_path": "/health",
        "keep_running": False, "preempt_predicted_fixes": True,
        "start_command": f"python -m uvicorn app:app --host 127.0.0.1 --port {DEMO_PORTS.get(target, 8000)}",
    }

    def gen():
        yield f"event: meta\ndata: {json.dumps({'target': target, 'label': tgt['label'], 'scenario': scenario, 'repo': DEMO_REPO, 'demo_runs_used': used + 1, 'demo_runs_limit': DEMO_RUN_LIMIT})}\n\n"
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
