"""Step 4: deploy attempt over SSM. Clone fresh, create venv, (optionally) apply a fix, install deps.

Every attempt starts from a clean clone + fresh venv so that a verified success is a real
reproduction, not a lucky leftover. Fix commands run AFTER the venv exists and BEFORE
`pip install`, so both system-level fixes (apt/dnf) and project-level fixes (sed on
requirements.txt, pip install X) take effect. The script is POSIX sh: SSM's
AWS-RunShellScript executes with /bin/sh, which is dash on Ubuntu.
"""
from __future__ import annotations

import re
import shlex

from app.aws.ssm import run_shell, parse_markers
from app.config import settings

EXIT_CLONE = 10
EXIT_VENV = 11
EXIT_INSTALL = 12
EXIT_FIX = 13


def project_slug(repo_url: str) -> str:
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.git$", "", name)
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name) or "project"


def workdir_for(repo_url: str) -> str:
    return f"{settings.target_workdir}/{project_slug(repo_url)}"


def _fix_block(run_id: str, stage: str, commands: list[str]) -> str:
    if not commands:
        return ""
    body = "\n".join(commands)
    return f"""
echo "__STAGE__=fix-{stage}"
cat > /tmp/nomeshops-fix-{stage}-{run_id}.sh <<'__NOMESHOPS_FIX__'
set -e
{body}
__NOMESHOPS_FIX__
bash /tmp/nomeshops-fix-{stage}-{run_id}.sh > "$FIXLOG" 2>&1
frc=$?
echo "__FIX_{stage.upper()}_RC__=$frc"
echo "__FIX_LOG_BEGIN__"; tail -c 6000 "$FIXLOG"; echo; echo "__FIX_LOG_END__"
if [ "$frc" -ne 0 ]; then echo "__RESULT__=fix_failed"; exit {EXIT_FIX}; fi
"""


def build_deploy_script(repo_url: str, branch: str | None, run_id: str,
                        pre_fixes: list[str] | None = None, post_fixes: list[str] | None = None) -> str:
    work = workdir_for(repo_url)
    branch_flag = f"--branch {shlex.quote(branch)}" if branch else ""
    pre_block = _fix_block(run_id, "pre", pre_fixes or [])
    post_block = _fix_block(run_id, "post", post_fixes or [])
    return f"""
export DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
WORK={shlex.quote(work)}
LOG=/tmp/nomeshops-{run_id}-install.log
FIXLOG=/tmp/nomeshops-{run_id}-fix.log
{pre_block}
echo "__STAGE__=clone"
# stop anything still running out of a previous attempt's venv, then start from a clean directory
for _p in /proc/[0-9]*; do if grep -qa "$WORK/.venv" "$_p/cmdline" 2>/dev/null; then kill "${{_p#/proc/}}" 2>/dev/null; fi; done; sleep 0.5
rm -rf "$WORK" 2>/dev/null; if [ -e "$WORK" ]; then sleep 1; rm -rf "$WORK"; fi
mkdir -p "$(dirname "$WORK")"
if ! git clone --depth 1 {branch_flag} {shlex.quote(repo_url)} "$WORK" > "$LOG" 2>&1; then
  tail -c 4000 "$LOG"; echo "__RESULT__=clone_failed"; exit {EXIT_CLONE}
fi
cd "$WORK" || exit {EXIT_CLONE}
echo "__STAGE__=venv"
if ! python3 -m venv .venv > "$LOG" 2>&1; then
  tail -c 4000 "$LOG"; echo "__RESULT__=venv_failed"; exit {EXIT_VENV}
fi
export VIRTUAL_ENV="$WORK/.venv"
export PATH="$WORK/.venv/bin:$PATH"
{post_block}
echo "__STAGE__=install"
if [ -f requirements.txt ]; then
  pip install -r requirements.txt > "$LOG" 2>&1; rc=$?
elif [ -f pyproject.toml ]; then
  pip install . > "$LOG" 2>&1; rc=$?
else
  echo "no requirements.txt or pyproject.toml; nothing to install" > "$LOG"; rc=0
fi
echo "__INSTALL_LOG_BEGIN__"; tail -c 14000 "$LOG"; echo; echo "__INSTALL_LOG_END__"
if [ "$rc" -ne 0 ]; then echo "__RESULT__=install_failed"; exit {EXIT_INSTALL}; fi
echo "__RESULT__=install_ok"
"""


def deploy_attempt(repo_url: str, branch: str | None, instance_id: str, run_id: str,
                   pre_fixes: list[str] | None = None, post_fixes: list[str] | None = None) -> dict:
    script = build_deploy_script(repo_url, branch, run_id, pre_fixes, post_fixes)
    res = run_shell(instance_id, script, comment=f"nomeshops deploy {run_id}")
    markers = parse_markers(res.stdout)
    result = markers.get("RESULT", "")
    return {
        "stage": "install",
        "ok": res.ok and result == "install_ok",
        "result": result or ("timeout" if res.status == "TimedOut" else f"ssm_{res.status.lower()}"),
        "exit_code": res.exit_code,
        "ssm_status": res.status,
        "command_id": res.command_id,
        "duration_s": res.duration_s,
        "fix_pre_rc": markers.get("FIX_PRE_RC"),
        "fix_post_rc": markers.get("FIX_POST_RC"),
        "failed_stage": markers.get("STAGE"),
        "stdout": res.stdout,
        "stderr": res.stderr,
    }
