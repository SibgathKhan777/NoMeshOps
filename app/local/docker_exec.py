"""Local executor: run the same shell scripts inside a Docker container instead of over SSM.
`instance_id` is the container name. Lets the whole loop run on a laptop with no AWS account."""
from __future__ import annotations

import subprocess
import time

from app.aws.ssm import CommandResult

MAX_OUT = 24000  # mirror SSM's truncation so behaviour matches


def run_shell_docker(container: str, script: str, timeout: int = 900, comment: str = "") -> CommandResult:
    start = time.time()
    probe = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return CommandResult(command_id="docker", status="Failed", exit_code=-1, stdout="",
                             stderr=f"container {container!r} is not running ({probe.stderr.strip()[:200]}). "
                                    f"Start targets with scripts/local_targets.sh", duration_s=0.0)
    try:
        proc = subprocess.run(["docker", "exec", "-i", container, "sh", "-s"], input=script, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return CommandResult(command_id="docker", status="TimedOut", exit_code=-1,
                             stdout=(e.stdout or b"")[-MAX_OUT:].decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")[-MAX_OUT:],
                             stderr="timed out", duration_s=round(time.time() - start, 2))
    return CommandResult(command_id="docker", status="Success" if proc.returncode == 0 else "Failed",
                         exit_code=proc.returncode, stdout=proc.stdout[-MAX_OUT:], stderr=proc.stderr[-MAX_OUT:],
                         duration_s=round(time.time() - start, 2))
