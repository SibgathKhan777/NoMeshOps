"""Step 2: fingerprint the target machine over SSM Run Command."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from app.aws.ssm import run_shell, parse_markers

FINGERPRINT_SCRIPT = r"""
echo "__KERNEL_NAME__=$(uname -s)"
echo "__ARCH__=$(uname -m)"
echo "__KERNEL__=$(uname -r)"
if [ -f /etc/os-release ]; then . /etc/os-release; fi
echo "__DISTRO__=${ID:-unknown}"
echo "__DISTRO_VERSION__=${VERSION_ID:-unknown}"
echo "__PRETTY__=${PRETTY_NAME:-unknown}"
echo "__PYTHON__=$(python3 --version 2>&1)"
echo "__PYTHON_PATH__=$(command -v python3 2>/dev/null)"
echo "__PIP__=$(python3 -m pip --version 2>&1 | head -1)"
echo "__VENV__=$(python3 -c 'import venv, ensurepip; print("ok")' 2>&1 | tail -1)"
echo "__GIT__=$(git --version 2>&1 | head -1)"
echo "__DOCKER__=$(docker --version 2>&1 | head -1)"
echo "__GCC__=$(gcc --version 2>&1 | head -1)"
echo "__PKG_MGR__=$(for _pm in apt-get dnf yum apk; do command -v "$_pm" >/dev/null 2>&1 && { echo "$_pm"; break; }; done)"
echo "__HOSTNAME__=$(hostname)"
echo "__MEM_MB__=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null)"
echo "__DISK_FREE_MB__=$(df -Pm / 2>/dev/null | awk 'NR==2 {print $4}')"
echo "__USER__=$(id -un)"
"""


@dataclass
class Fingerprint:
    instance_id: str
    os: str = "unknown"              # distro id, e.g. ubuntu, amzn
    os_version: str = "unknown"      # e.g. 22.04, 2023
    os_pretty: str = ""
    arch: str = "unknown"            # x86_64 / aarch64
    kernel: str = ""
    python_version: str = ""         # full, e.g. 3.9.16 ('' if missing)
    python_path: str = ""
    pip_version: str = ""
    has_pip: bool = False
    has_venv: bool = False
    has_git: bool = False
    has_docker: bool = False
    has_gcc: bool = False
    package_manager: str = ""        # apt-get / dnf / yum / apk
    hostname: str = ""
    mem_mb: int = 0
    disk_free_mb: int = 0
    user: str = ""
    raw: dict | None = None

    @property
    def python_minor(self) -> str:
        m = re.match(r"^(\d+\.\d+)", self.python_version)
        return m.group(1) if m else ""

    @property
    def os_key(self) -> str:
        """Coarse OS key used in error signatures, e.g. 'ubuntu22' or 'amzn2023'."""
        major = self.os_version.split(".")[0]
        return f"{self.os}{major}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["python_minor"] = self.python_minor
        d["os_key"] = self.os_key
        return d


def parse_fingerprint(instance_id: str, stdout: str) -> Fingerprint:
    m = parse_markers(stdout)
    fp = Fingerprint(instance_id=instance_id, raw=m)
    fp.os = m.get("DISTRO", "unknown")
    fp.os_version = m.get("DISTRO_VERSION", "unknown")
    fp.os_pretty = m.get("PRETTY", "")
    fp.arch = m.get("ARCH", "unknown")
    fp.kernel = m.get("KERNEL", "")
    py = re.search(r"Python (\d+\.\d+\.\d+)", m.get("PYTHON", ""))
    fp.python_version = py.group(1) if py else ""
    fp.python_path = m.get("PYTHON_PATH", "")
    pip = re.search(r"pip (\d+(?:\.\d+)+)", m.get("PIP", ""))
    fp.pip_version = pip.group(1) if pip else ""
    fp.has_pip = bool(pip)
    fp.has_venv = m.get("VENV", "") == "ok"
    fp.has_git = "git version" in m.get("GIT", "")
    fp.has_docker = "Docker version" in m.get("DOCKER", "")
    fp.has_gcc = "gcc" in m.get("GCC", "").lower() and "not found" not in m.get("GCC", "")
    pm = m.get("PKG_MGR", "")
    fp.package_manager = pm.rsplit("/", 1)[-1] if pm else ""
    fp.hostname = m.get("HOSTNAME", "")
    fp.mem_mb = int(m.get("MEM_MB") or 0)
    fp.disk_free_mb = int(m.get("DISK_FREE_MB") or 0)
    fp.user = m.get("USER", "")
    return fp


def fingerprint_instance(instance_id: str) -> tuple[Fingerprint, dict]:
    result = run_shell(instance_id, FINGERPRINT_SCRIPT, timeout=120, comment="nomeshops fingerprint")
    fp = parse_fingerprint(instance_id, result.stdout)
    return fp, result.to_dict()
