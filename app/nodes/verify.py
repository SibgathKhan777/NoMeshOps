"""Step 7: real verification. Import check -> app start -> HTTP health check. Never trust exit codes alone."""
from __future__ import annotations

import json
import shlex

from app.aws.ssm import run_shell, parse_markers
from app.nodes.deploy import workdir_for

IMPORT_CHECK_PY = r'''
import importlib, json, sys
try:
    import importlib.metadata as md
except ImportError:
    import importlib_metadata as md  # noqa
reqs = json.loads(sys.argv[1])
# Prefer the manifest as it exists on the box: a fix may have edited requirements.txt after the orchestrator scanned it.
try:
    import re, os
    if os.path.isfile("requirements.txt"):
        on_box = []
        for raw in open("requirements.txt", encoding="utf-8", errors="replace"):
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m:
                on_box.append(m.group(1).lower().replace("_", "-"))
        if on_box:
            reqs = on_box
except Exception:
    pass
missing, import_fail, imported = [], [], []
try:
    pkg_to_mods = md.packages_distributions()
    rev = {}
    for mod, dists in pkg_to_mods.items():
        for d in dists:
            rev.setdefault(d.lower().replace("_", "-"), []).append(mod)
except Exception:
    rev = {}
for r in reqs:
    try:
        dist = md.distribution(r)
    except md.PackageNotFoundError:
        missing.append(r); continue
    mods = rev.get(r) or []
    if not mods:
        top = dist.read_text("top_level.txt") or ""
        mods = [m for m in top.split() if m and not m.startswith("_")]
    if not mods:
        mods = [r.replace("-", "_")]
    mods = [m for m in mods if not m.startswith("_") and m not in ("tests", "test")]
    if not mods:
        imported.append(r); continue
    mod = sorted(mods, key=len)[0]
    try:
        importlib.import_module(mod)
        imported.append(f"{r} ({mod})")
    except Exception as e:  # noqa
        import_fail.append(f"{r}: {type(e).__name__}: {str(e)[:200]}")
print("__IMPORT__=" + ("OK" if not missing and not import_fail else "FAIL"))
print("__IMPORT_DETAIL__=" + json.dumps({"missing": missing, "import_fail": import_fail, "imported": imported}))
'''

HEALTH_CHECK_PY = r'''
import sys, urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=3) as r:
        body = r.read(500).decode("utf-8", "replace")
        print("__HEALTH_STATUS__=%d" % r.status)
        print("__HEALTH_BODY__=" + body.replace("\n", " "))
        sys.exit(0 if 200 <= r.status < 400 else 1)
except Exception as e:
    print("__HEALTH_ERR__=" + type(e).__name__ + ": " + str(e)[:200])
    sys.exit(1)
'''


def build_verify_script(repo_url: str, run_id: str, dep_names: list[str], start_command: str,
                        port: int, health_path: str, keep_running: bool) -> str:
    work = workdir_for(repo_url)
    reqs_json = shlex.quote(json.dumps(dep_names))
    url = f"http://127.0.0.1:{port}{health_path}"
    kill_block = "" if keep_running else '''
kill "$APP_PID" 2>/dev/null; sleep 1; kill -9 "$APP_PID" 2>/dev/null
for _p in /proc/[0-9]*; do if grep -qa "$WORK/.venv" "$_p/cmdline" 2>/dev/null; then kill "${_p#/proc/}" 2>/dev/null; fi; done; true
echo "__APP_STOPPED__=1"
'''
    return f"""
WORK={shlex.quote(work)}
cd "$WORK" || {{ echo "__RESULT__=workdir_missing"; exit 20; }}
export VIRTUAL_ENV="$WORK/.venv"
export PATH="$WORK/.venv/bin:$PATH"
export PYTHONUNBUFFERED=1
echo "__STAGE__=import"
cat > /tmp/nomeshops-{run_id}-import.py <<'__NOMESHOPS_PY__'
{IMPORT_CHECK_PY}
__NOMESHOPS_PY__
python /tmp/nomeshops-{run_id}-import.py {reqs_json} 2>&1 | tail -c 6000
echo "__STAGE__=start"
APPLOG=/tmp/nomeshops-{run_id}-app.log
# stop anything from a previous run on this port
(fuser -k {port}/tcp >/dev/null 2>&1 || true)
# exec so $APP_PID is the app itself, not a wrapper shell (otherwise kill leaves the app running)
nohup sh -c {shlex.quote("exec " + start_command)} > "$APPLOG" 2>&1 &
APP_PID=$!
echo "__APP_PID__=$APP_PID"
cat > /tmp/nomeshops-{run_id}-health.py <<'__NOMESHOPS_PY__'
{HEALTH_CHECK_PY}
__NOMESHOPS_PY__
i=0; ok=0
while [ $i -lt 40 ]; do
  if python /tmp/nomeshops-{run_id}-health.py {shlex.quote(url)} > /tmp/nomeshops-{run_id}-health.out 2>&1; then ok=1; break; fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then echo "__APP_EXITED__=1"; break; fi
  sleep 1; i=$((i+1))
done
cat /tmp/nomeshops-{run_id}-health.out
if [ "$ok" = "1" ]; then echo "__HEALTH__=OK"; else echo "__HEALTH__=FAIL"; fi
echo "__APP_LOG_BEGIN__"; tail -c 4000 "$APPLOG"; echo; echo "__APP_LOG_END__"
{kill_block}
if [ "$ok" = "1" ]; then echo "__RESULT__=verified"; exit 0; else echo "__RESULT__=health_failed"; exit 21; fi
"""


def verify_deploy(repo_url: str, instance_id: str, run_id: str, dep_names: list[str], start_command: str,
                  port: int, health_path: str, keep_running: bool) -> dict:
    script = build_verify_script(repo_url, run_id, dep_names, start_command, port, health_path, keep_running)
    res = run_shell(instance_id, script, timeout=300, comment=f"nomeshops verify {run_id}")
    m = parse_markers(res.stdout)
    detail = {}
    try:
        detail = json.loads(m.get("IMPORT_DETAIL", "{}"))
    except json.JSONDecodeError:
        pass
    import_ok = m.get("IMPORT") == "OK"
    health_ok = m.get("HEALTH") == "OK"
    started = "APP_PID" in m and m.get("APP_EXITED") != "1"
    stage = "verified" if (import_ok and health_ok) else ("import" if not import_ok else ("start" if not started else "health"))
    return {
        "stage": stage,
        "ok": import_ok and health_ok and res.ok,
        "install_ok": True,
        "import_ok": import_ok,
        "import_detail": detail,
        "app_started": started,
        "health_ok": health_ok,
        "health_status": m.get("HEALTH_STATUS"),
        "health_body": m.get("HEALTH_BODY"),
        "health_error": m.get("HEALTH_ERR"),
        "result": m.get("RESULT", f"ssm_{res.status.lower()}"),
        "exit_code": res.exit_code,
        "ssm_status": res.status,
        "command_id": res.command_id,
        "duration_s": res.duration_s,
        "stdout": res.stdout,
        "stderr": res.stderr,
    }
