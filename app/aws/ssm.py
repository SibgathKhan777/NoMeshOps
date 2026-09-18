"""Run a shell script on an EC2 instance through SSM Run Command and wait for the result."""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict

from app.aws.session import client
from app.config import settings

TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed", "Cancelling"}


@dataclass
class CommandResult:
    command_id: str
    status: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.status == "Success" and self.exit_code == 0

    def to_dict(self) -> dict:
        return asdict(self)


def run_shell(
    instance_id: str,
    script: str,
    timeout: int | None = None,
    comment: str = "",
) -> CommandResult:
    """Execute `script` with AWS-RunShellScript. Polls until the invocation is terminal.

    stdout/stderr are truncated by SSM at 24 000 chars each; scripts should tail their
    own logs. If LOGS_BUCKET is set the full output is also delivered to S3 by SSM.
    """
    timeout = timeout or settings.ssm_timeout_seconds
    ssm = client("ssm")
    kwargs = dict(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": script.splitlines(), "executionTimeout": [str(timeout)]},
        Comment=comment[:100],
        TimeoutSeconds=60,
    )
    if settings.logs_bucket:
        kwargs["OutputS3BucketName"] = settings.logs_bucket
        kwargs["OutputS3KeyPrefix"] = "ssm-output"
    start = time.time()
    resp = ssm.send_command(**kwargs)
    cid = resp["Command"]["CommandId"]

    delay = 1.0
    while True:
        try:
            inv = ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(delay)
            continue
        if inv["Status"] in TERMINAL:
            break
        if time.time() - start > timeout + 90:
            raise TimeoutError(f"SSM command {cid} did not finish within {timeout}s")
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)

    return CommandResult(
        command_id=cid,
        status=inv["Status"],
        exit_code=int(inv.get("ResponseCode", -1)),
        stdout=inv.get("StandardOutputContent", "") or "",
        stderr=inv.get("StandardErrorContent", "") or "",
        duration_s=round(time.time() - start, 2),
    )


def parse_markers(text: str) -> dict[str, str]:
    """Extract `__KEY__=value` lines that our scripts print for structured results."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("__") and "=" in line:
            key, _, value = line.partition("=")
            if key.endswith("__"):
                out[key.strip("_")] = value.strip()
    return out
