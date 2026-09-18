"""Step 1: scan the target project (requirements / pyproject / Dockerfile) with a local shallow clone."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field, asdict

from packaging.requirements import Requirement, InvalidRequirement

FRAMEWORK_HINTS = ["fastapi", "flask", "django", "starlette", "sanic", "aiohttp"]


@dataclass
class Dependency:
    name: str
    specifier: str = ""
    raw: str = ""
    extras: list[str] = field(default_factory=list)


@dataclass
class ProjectScan:
    repo_url: str
    branch: str | None
    ok: bool = False
    error: str = ""
    has_requirements: bool = False
    has_pyproject: bool = False
    has_dockerfile: bool = False
    python_requires: str = ""
    dependencies: list[Dependency] = field(default_factory=list)
    entrypoint_candidates: list[str] = field(default_factory=list)
    framework: str = ""
    file_count: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def dependency_names(self) -> list[str]:
        return [d.name for d in self.dependencies]


def parse_requirements_text(text: str) -> list[Dependency]:
    deps: list[Dependency] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--")):
            continue
        # strip environment markers / hashes
        line = line.split(";", 1)[0].split("\\", 1)[0].strip()
        try:
            r = Requirement(line)
            deps.append(
                Dependency(
                    name=r.name.lower().replace("_", "-"),
                    specifier=str(r.specifier),
                    raw=raw.strip(),
                    extras=sorted(r.extras),
                )
            )
        except InvalidRequirement:
            m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
            if m:
                deps.append(Dependency(name=m.group(1).lower(), raw=raw.strip()))
    return deps


def _detect_framework(deps: list[Dependency]) -> str:
    names = {d.name for d in deps}
    for fw in FRAMEWORK_HINTS:
        if fw in names:
            return fw
    return ""


def scan_repo(repo_url: str, branch: str | None = None) -> ProjectScan:
    scan = ProjectScan(repo_url=repo_url, branch=branch)
    tmp = tempfile.mkdtemp(prefix="nomeshops-scan-")
    try:
        cmd = ["git", "clone", "--depth", "1", "--quiet"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [repo_url, tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            scan.error = (proc.stderr or proc.stdout).strip()[-500:]
            return scan

        req = os.path.join(tmp, "requirements.txt")
        pyp = os.path.join(tmp, "pyproject.toml")
        scan.has_requirements = os.path.isfile(req)
        scan.has_pyproject = os.path.isfile(pyp)
        scan.has_dockerfile = os.path.isfile(os.path.join(tmp, "Dockerfile"))

        if scan.has_requirements:
            with open(req, encoding="utf-8", errors="replace") as f:
                scan.dependencies = parse_requirements_text(f.read())
        if scan.has_pyproject:
            with open(pyp, "rb") as f:
                try:
                    data = tomllib.load(f)
                except tomllib.TOMLDecodeError as e:
                    data = {}
                    scan.error = f"pyproject.toml parse error: {e}"
            project = data.get("project", {})
            scan.python_requires = project.get("requires-python", "") or scan.python_requires
            if not scan.dependencies:
                scan.dependencies = parse_requirements_text("\n".join(project.get("dependencies", [])))

        for candidate in (".python-version", "runtime.txt"):
            p = os.path.join(tmp, candidate)
            if os.path.isfile(p) and not scan.python_requires:
                v = open(p).read().strip().replace("python-", "")
                if re.match(r"^\d+\.\d+", v):
                    scan.python_requires = f"=={v}.*" if v.count(".") == 1 else f"=={v}"

        for name in ("app.py", "main.py", "server.py", "wsgi.py", "asgi.py", "manage.py"):
            if os.path.isfile(os.path.join(tmp, name)):
                scan.entrypoint_candidates.append(name)
        scan.file_count = sum(len(files) for _, _, files in os.walk(tmp))
        scan.framework = _detect_framework(scan.dependencies)
        scan.ok = True
        return scan
    except Exception as e:  # noqa: BLE001
        scan.error = f"{type(e).__name__}: {e}"
        return scan
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def guess_start_command(scan: dict, port: int) -> str:
    """Best-effort default start command; the request can always override it."""
    fw = scan.get("framework", "")
    entry = "app"
    for c in scan.get("entrypoint_candidates", []):
        if c in ("app.py", "main.py"):
            entry = c[:-3]
            break
    if fw == "flask":
        return f"python -m flask --app {entry} run --host 127.0.0.1 --port {port}"
    if fw == "django":
        return f"python manage.py runserver 127.0.0.1:{port}"
    # FastAPI / Starlette / unknown: assume an ASGI `app` object
    return f"python -m uvicorn {entry}:app --host 127.0.0.1 --port {port}"
