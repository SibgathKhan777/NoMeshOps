import os

from app.local import kb_local
from app.nodes import rules, store
from app.nodes.fingerprint import parse_fingerprint


def test_local_kb_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("app.local.kb_local.settings", type("S", (), {"local_data_dir": str(tmp_path)})())
    err = {"signature": "sig1", "family": "fam1", "error_type": "ResolutionError", "package": "reqests"}
    fp = {"os_key": "ubuntu22", "arch": "x86_64", "python_minor": "3.10"}
    assert kb_local.get_fix("sig1") is None
    kb_local.put_fix(err, fp, "sed -i s/reqests/requests/ requirements.txt", "typo", "llm-anthropic")
    assert kb_local.get_fix("sig1")["fix_command"].startswith("sed")
    kb_local.record_outcome("sig1", True)
    assert kb_local.get_fix("sig1")["success_count"] == 2
    assert kb_local.get_family_fixes("fam1")[0]["error_signature"] == "sig1"
    kb_local.delete_fix("sig1")
    assert kb_local.list_fixes() == []


def test_local_store_writes_file(tmp_path, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(store, "settings", replace(store.settings, store_backend="local", local_data_dir=str(tmp_path)))
    path = store.store_attempt({"instance_id": "c1", "run_id": "r1", "x": 1})
    assert os.path.isfile(path)
    assert store.list_attempts()[0]["key"] == path


def test_bare_container_gets_python_and_git_bootstrap():
    fp = parse_fingerprint("nomeshops-ubuntu22", "__DISTRO__=ubuntu\n__DISTRO_VERSION__=22.04\n__ARCH__=x86_64\n"
                           "__PYTHON__=sh: 1: python3: not found\n__PIP__=sh: 1: python3: not found\n__VENV__=sh: 1: python3: not found\n"
                           "__GIT__=sh: 1: git: not found\n__GCC__=sh: 1: gcc: not found\n__PKG_MGR__=/usr/bin/apt-get\n").to_dict()
    issues = rules.check_compatibility({"python_requires": ">=3.8", "dependencies": []}, fp)
    names = [i.rule for i in issues]
    assert "python-missing" in names and "git-missing" in names
    assert "pip-or-venv-missing" not in names, "pip rule must not duplicate the python-missing bootstrap"
    assert all(i.fix_command and i.fix_command.startswith("export DEBIAN_FRONTEND") for i in issues if i.rule in ("python-missing", "git-missing"))
