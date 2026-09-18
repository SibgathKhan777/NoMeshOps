from app.graph import run_deploy, summarize
from app.nodes import rules, signature
from app.nodes.scan import parse_requirements_text
from app.nodes.fingerprint import parse_fingerprint


def _sources(state):
    return [f["source"] for f in state["fix_chain"] if f.get("signature")]


def test_rules_path_fixes_pg_config_without_llm(fakes, request_payload):
    st = run_deploy(request_payload)
    s = summarize(st)
    assert s["deploy_success"] and s["final_status"] == "success"
    assert _sources(st) == ["rules"]
    assert fakes["bedrock"].calls == 0
    assert fakes["kb"].items, "verified rules fix should be stored in the knowledge base"
    assert s["s3_key"] == "attempts/fake.json"
    assert len(s["attempts"]) == 2 and s["attempts"][0]["ok"] is False and s["attempts"][1]["ok"] is True


def test_novel_error_goes_to_bedrock_then_kb_on_second_run(fakes, request_payload):
    box = fakes["box"]
    box.fail_text = "error: subprocess-exited-with-error\n  Building wheel for numpy (pyproject.toml) did not run successfully.\nERROR: Failed building wheel for numpy"
    box.failing_pkg = "numpy"
    box.fix_marker = "numpy>=1.26"
    fakes["bedrock"].fix_command = "sed -i 's/numpy==1.19.5/numpy>=1.26/' requirements.txt"

    st1 = run_deploy(request_payload)
    assert st1["deploy_success"]
    assert _sources(st1) == ["bedrock"], _sources(st1)
    assert fakes["bedrock"].calls == 1
    sig = st1["fix_chain"][-1]["signature"]
    assert sig in fakes["kb"].items

    # second machine, same platform, same error class -> KB hit, no LLM
    st2 = run_deploy({**request_payload, "instance_id": "i-1second"})
    assert st2["deploy_success"]
    assert _sources(st2) == ["knowledge-exact"]
    assert fakes["bedrock"].calls == 1, "Bedrock must not be called on a knowledge-base hit"
    assert fakes["kb"].items[sig]["success_count"] == 2


def test_stop_condition_when_nothing_works(fakes, request_payload):
    box = fakes["box"]
    box.fail_text = "ERROR: No matching distribution found for reqests==9.9.9"
    box.failing_pkg = "reqests"
    box.fix_marker = "THIS-NEVER-APPEARS"
    st = run_deploy(request_payload)
    assert not st["deploy_success"] and st["final_status"] == "failed"
    assert fakes["bedrock"].calls == 1, "exactly one LLM-assisted retry"
    assert st["failure_reason"]
    assert not fakes["kb"].items, "unverified fixes must never be stored"


def test_predicted_git_missing_is_preempted(fakes, request_payload):
    fakes["box"].has_git = False
    st = run_deploy({**request_payload, "preempt_predicted_fixes": True})
    assert any(i["rule"] == "git-missing" for i in st["predicted_issues"])
    assert "apt-get install -y -qq git" in fakes["box"].scripts[1]
    assert st["deploy_success"]


def test_deterministic_check_is_offline_and_fast():
    fp = parse_fingerprint("i-x", "__DISTRO__=amzn\n__DISTRO_VERSION__=2023\n__ARCH__=aarch64\n__PYTHON__=Python 3.9.16\n"
                                  "__PIP__=pip 21 from /x\n__VENV__=ok\n__GIT__=git version 2.40\n__GCC__=gcc 11\n__PKG_MGR__=/usr/bin/dnf\n").to_dict()
    scan = {"python_requires": ">=3.12", "dependencies": [vars(d) for d in parse_requirements_text("numpy==2.1.0\npsycopg2\n")],
            "has_dockerfile": True}
    issues = rules.check_compatibility(scan, fp)
    names = {i.rule for i in issues}
    assert {"python-version-mismatch", "package-needs-newer-python", "system-dependency", "dockerfile-without-docker"} <= names
    sysdep = next(i for i in issues if i.rule == "system-dependency")
    assert sysdep.fix_command.startswith("dnf install")


def test_signature_is_stable_and_context_aware():
    fp_a = {"os_key": "ubuntu22", "arch": "x86_64", "python_minor": "3.10"}
    fp_b = {"os_key": "amzn2023", "arch": "x86_64", "python_minor": "3.9"}
    out = "Collecting psycopg2==2.9.9\nError: pg_config executable not found.\nERROR: Failed building wheel for psycopg2"
    e1 = signature.compute_signature(signature.classify(out, "", "install"), fp_a)
    e2 = signature.compute_signature(signature.classify(out, "", "install"), fp_a)
    e3 = signature.compute_signature(signature.classify(out.replace("/tmp/pip-abc", "/tmp/pip-xyz"), "", "install"), fp_b)
    assert e1.signature == e2.signature
    assert e1.signature != e3.signature and e1.family == e3.family
    assert e1.error_type == "BuildError" and e1.package == "psycopg2" and e1.package_version == "==2.9.9"


def test_busy_target_fails_fast_without_fix_ladder(fakes, request_payload, monkeypatch):
    from app.aws.ssm import CommandResult
    box = fakes["box"]
    orig = box.run

    def run(script):
        if "__STAGE__=clone" in script and "LOCK=" in script:
            return CommandResult("c-dep", "Failed", 14, "__LOCK_OWNER__=deadbeef\n__LOCK_AGE__=42\n__RESULT__=locked\n", "", 0.2)
        return orig(script)
    box.run = run
    st = run_deploy(request_payload)
    assert st["final_status"] == "failed" and not st["deploy_success"]
    assert st["current_error"]["error_type"] == "TargetBusy"
    assert "deadbeef" in st["failure_reason"]
    assert fakes["bedrock"].calls == 0 and not fakes["kb"].items
    assert len(st["attempts"]) == 1
