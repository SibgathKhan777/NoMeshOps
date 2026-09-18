"""Phase 2 smoke test.
 Part 1 (offline, ms): deterministic checker flags python_requires >=3.12 vs a 3.9 fingerprint with NO AWS calls.
 Part 2 (real AWS):    seed a fix for the signature your instance produces, re-run, assert the knowledge-base path is taken.
Usage: python scripts/smoke_phase2.py                       # part 1 only
       python scripts/smoke_phase2.py --instance i-... --repo URL --fix "<fix command>"   # parts 1 + 2
"""
import argparse, sys, time
sys.path.insert(0, ".")
import boto3
from app.nodes import rules
from app.nodes.scan import parse_requirements_text

# ---- part 1: offline
orig_client = boto3.client
boto3.client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("deterministic check must not touch AWS"))
t0 = time.perf_counter()
fp = {"os": "amzn", "os_version": "2023", "os_key": "amzn2023", "arch": "x86_64", "python_version": "3.9.16", "python_minor": "3.9",
      "has_pip": True, "has_venv": True, "has_git": True, "has_gcc": True, "package_manager": "dnf", "disk_free_mb": 8000}
scan = {"python_requires": ">=3.12", "dependencies": [vars(d) for d in parse_requirements_text("fastapi\nnumpy==2.1.0\n")], "has_dockerfile": False}
issues = rules.check_compatibility(scan, fp)
ms = (time.perf_counter() - t0) * 1000
boto3.client = orig_client
assert any(i.rule == "python-version-mismatch" for i in issues), issues
assert any(i.rule == "package-needs-newer-python" and i.package == "numpy" for i in issues), issues
assert ms < 50, f"too slow: {ms} ms"
print(f"part 1: flagged {len(issues)} issue(s) offline in {ms:.2f} ms -> PASS")

p = argparse.ArgumentParser()
p.add_argument("--instance"); p.add_argument("--repo"); p.add_argument("--fix")
a = p.parse_args()
if not a.instance:
    print("PHASE 2 SMOKE (part 1 only): PASS"); sys.exit(0)

# ---- part 2: real
from app.graph import run_deploy, summarize
from app.nodes import knowledge
req = {"repo_url": a.repo, "instance_id": a.instance, "app_port": 8000, "health_path": "/health", "preempt_predicted_fixes": True}
print("part 2: first run to learn the failure signature (Bedrock is allowed to run here)...")
s1 = summarize(run_deploy(req))
failing = [x for x in s1["attempts"] if x.get("error")]
assert failing, "the target project did not fail; pick a repo that fails on this instance"
sig = failing[0]["error"]["signature"]
fix = a.fix or (s1["fix_applied"] or {}).get("fix_command")
assert fix, "no fix known; pass --fix"
knowledge.delete_fix(sig)
knowledge.put_fix(failing[0]["error"], s1["fingerprint"], fix, "seeded by smoke_phase2", "seed")
print(f"seeded fix for {sig}; re-running...")
s2 = summarize(run_deploy(req))
sources = [f["source"] for f in (s2.get("fix_applied") and [s2["fix_applied"]] or [])]
kb_events = [e for e in s2["events"] if e["node"] == "knowledge" and "HIT" in e["msg"]]
llm_events = [e for e in s2["events"] if e["node"] == "bedrock"]
assert kb_events, "knowledge-base path not taken"
assert not llm_events, "Bedrock was called despite a seeded fix"
assert s2["deploy_success"], s2["failure_reason"]
print("part 2: knowledge-base hit applied and verified, no Bedrock call -> PASS")
print("PHASE 2 SMOKE: PASS")
