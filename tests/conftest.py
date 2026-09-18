"""Offline fakes: a simulated EC2 box behind SSM, an in-memory DynamoDB, a scripted Bedrock.
No AWS credentials or network are needed for these tests."""
from __future__ import annotations

import re
import time

import pytest

from app.aws.ssm import CommandResult
from app.nodes import knowledge as kb_mod


class FakeBox:
    """Simulates an instance: fingerprint output, a deploy that fails until a fix is present."""

    def __init__(self, os_id="ubuntu", ver="22.04", py="3.10.12", failing_pkg="psycopg2",
                 fail_text="Error: pg_config executable not found.\nERROR: Failed building wheel for psycopg2",
                 fix_marker="libpq-dev", has_git=True, has_venv=True):
        self.os_id, self.ver, self.py = os_id, ver, py
        self.failing_pkg, self.fail_text, self.fix_marker = failing_pkg, fail_text, fix_marker
        self.has_git, self.has_venv = has_git, has_venv
        self.scripts: list[str] = []

    def fingerprint(self) -> str:
        return (f"__DISTRO__={self.os_id}\n__DISTRO_VERSION__={self.ver}\n__PRETTY__={self.os_id} {self.ver}\n"
                f"__ARCH__=x86_64\n__PYTHON__=Python {self.py}\n__PIP__=pip 23.0 from /x\n"
                f"__VENV__={'ok' if self.has_venv else 'No module named ensurepip'}\n"
                f"__GIT__={'git version 2.34' if self.has_git else 'sh: git: not found'}\n"
                f"__GCC__=gcc 11.4\n__PKG_MGR__=/usr/bin/apt-get\n__DISK_FREE_MB__=9000\n")

    def run(self, script: str) -> CommandResult:
        self.scripts.append(script)
        if "__KERNEL_NAME__" in script:
            return CommandResult("c-fp", "Success", 0, self.fingerprint(), "", 0.5)
        if "__STAGE__=import" in script:
            return CommandResult("c-verify", "Success", 0,
                                 '__IMPORT__=OK\n__IMPORT_DETAIL__={"missing":[],"import_fail":[],"imported":["fastapi"]}\n'
                                 "__APP_PID__=123\n__HEALTH_STATUS__=200\n__HEALTH_BODY__={\"ok\":true}\n__HEALTH__=OK\n__RESULT__=verified\n", "", 1.0)
        # deploy script
        fixed = self.fix_marker in script
        if not self.has_git and "apt-get install -y -qq git" not in script:
            return CommandResult("c-dep", "Failed", 10, "__STAGE__=clone\nsh: 1: git: not found\n__RESULT__=clone_failed\n", "", 0.3)
        if fixed:
            return CommandResult("c-dep", "Success", 0, f"__STAGE__=install\nCollecting {self.failing_pkg}==2.9.9\nSuccessfully installed\n__RESULT__=install_ok\n", "", 2.0)
        return CommandResult("c-dep", "Failed", 12,
                             f"__STAGE__=install\n__INSTALL_LOG_BEGIN__\nCollecting {self.failing_pkg}==2.9.9\n{self.fail_text}\n__INSTALL_LOG_END__\n__RESULT__=install_failed\n",
                             "", 2.0)


class FakeKB:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.reads = 0

    def get_fix(self, sig):
        self.reads += 1
        return self.items.get(sig)

    def get_family_fixes(self, fam):
        return sorted([i for i in self.items.values() if i.get("error_family") == fam],
                      key=lambda i: i.get("success_count", 0), reverse=True)

    def put_fix(self, error, fp, cmd, desc, source):
        item = {"error_signature": error["signature"], "error_family": error.get("family", ""), "fix_command": cmd,
                "description": desc, "source": source, "success_count": 1, "failure_count": 0,
                "os": fp.get("os_key"), "python_version": fp.get("python_minor"), "first_seen_at": "2026-09-18T00:00:00"}
        self.items[error["signature"]] = item
        return item

    def record_outcome(self, sig, ok):
        f = "success_count" if ok else "failure_count"
        self.items[sig][f] = self.items[sig].get(f, 0) + 1


class FakeBedrock:
    def __init__(self, fix_command="apt-get install -y -qq libpq-dev", stage="post"):
        self.calls = 0
        self.fix_command, self.stage = fix_command, stage

    def propose(self, fingerprint, scan, error, output, prior, hint=None):
        self.calls += 1
        return {"source": "bedrock", "fix_command": self.fix_command, "stage": self.stage,
                "description": "fake fix", "confidence": 0.9, "rationale": "because", "model_id": "fake",
                "input_tokens": 10, "output_tokens": 5}


@pytest.fixture
def fakes(monkeypatch):
    box, kb, br = FakeBox(), FakeKB(), FakeBedrock()
    stored = {}

    monkeypatch.setattr("app.nodes.fingerprint.run_shell", lambda iid, script, **kw: box.run(script))
    monkeypatch.setattr("app.nodes.deploy.run_shell", lambda iid, script, **kw: box.run(script))
    monkeypatch.setattr("app.nodes.verify.run_shell", lambda iid, script, **kw: box.run(script))
    monkeypatch.setattr(kb_mod, "get_fix", kb.get_fix)
    monkeypatch.setattr(kb_mod, "get_family_fixes", kb.get_family_fixes)
    monkeypatch.setattr(kb_mod, "put_fix", kb.put_fix)
    monkeypatch.setattr(kb_mod, "record_outcome", kb.record_outcome)
    monkeypatch.setattr("app.graph.propose_fix", br.propose)
    monkeypatch.setattr("app.graph.store_attempt", lambda rec: stored.setdefault("key", "attempts/fake.json"))

    from app.nodes.scan import ProjectScan, Dependency
    def fake_scan(repo_url, branch=None):
        return ProjectScan(repo_url=repo_url, branch=branch, ok=True, has_requirements=True, framework="fastapi",
                           dependencies=[Dependency("fastapi", ">=0.100", "fastapi>=0.100"),
                                         Dependency("psycopg2", "==2.9.9", "psycopg2==2.9.9")],
                           entrypoint_candidates=["app.py"], file_count=3)
    monkeypatch.setattr("app.graph.scan_repo", fake_scan)
    return {"box": box, "kb": kb, "bedrock": br, "stored": stored}


@pytest.fixture
def request_payload():
    return {"repo_url": "https://github.com/example/demo.git", "instance_id": "i-0fake", "app_port": 8000,
            "health_path": "/health", "keep_running": False, "preempt_predicted_fixes": False}
