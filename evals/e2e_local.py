"""End-to-end evaluation in local Docker mode. Runs a scenario list sequentially, records the outcome of each,
and writes evals/results/e2e_local.json. Each scenario states what a correct system should do."""
import json, os, subprocess, sys, time, shutil
sys.path.insert(0, ".")
os.environ.update({"EXECUTOR": "docker", "KB_BACKEND": "local", "STORE_BACKEND": "local", "LLM_BACKEND": "none",
                   "LOCAL_DATA_DIR": ".nomeshops-eval"})
shutil.rmtree(".nomeshops-eval", ignore_errors=True)
from app.graph import run_deploy, summarize
from app.local import kb_local

REPO = "https://github.com/SibgathKhan777/nomeshops-sample.git"
U, A = "nomeshops-ubuntu22", "nomeshops-al2023"

def seed_typo_fix():
    # the fix that AWS/local runs learned earlier, keyed by the exact ubuntu22/x86_64/3.10 signature
    err = {"signature": "44ed471b492e719f2437", "family": "a8b9365f7acb175681e0", "error_type": "ResolutionError",
           "package": "reqests", "package_version": "==2.31.0", "normalized": "no matching distribution found for reqests"}
    fp = {"os_key": "ubuntu22", "os_pretty": "Ubuntu 22.04.5 LTS", "arch": "x86_64", "python_minor": "3.10"}
    kb_local.put_fix(err, fp, "sed -i 's/reqests==2.31.0/requests==2.31.0/' requirements.txt", "Fix typo reqests->requests", "seed")

SCENARIOS = [
    # name, request overrides, expectation dict, setup
    ("clean project installs first try", dict(branch="eval-clean", instance_id=U),
     dict(success=True, attempts=1, fix_sources=[], llm_calls=0), None),
    ("typo dep: knowledge-base hit (exact, seeded)", dict(branch=None, instance_id=U),
     dict(success=True, attempts=2, fix_sources=["knowledge-exact"], llm_calls=0), seed_typo_fix),
    ("typo dep on AL2023: family match from ubuntu fix", dict(branch=None, instance_id=A),
     dict(success=True, attempts=2, fix_sources=["knowledge-family"], llm_calls=0), None),
    ("psycopg2 source build, preempt on: rules predict + pre-install libpq", dict(branch="eval-psycopg2", instance_id=U, preempt_predicted_fixes=True),
     dict(success=True, attempts=1, fix_sources=[], predicted_rules=["system-dependency"], llm_calls=0), None),
    ("pyproject-only manifest", dict(branch="eval-pyproject", instance_id=U),
     dict(success=True, attempts=1, fix_sources=[], llm_calls=0), None),
    ("flask app: start command auto-detected", dict(branch="eval-flask", instance_id=U),
     dict(success=True, attempts=1, fix_sources=[], llm_calls=0, start_contains="flask"), None),
    ("app crashes at startup: verification catches it, clean failure", dict(branch="eval-start-crash", instance_id=U),
     dict(success=False, final="failed", stage_in=("start", "health"), llm_calls=1), None),
    ("undeclared runtime import: import gate catches it", dict(branch="eval-missing-runtime-dep", instance_id=U),
     # relabelled: the import gate only covers DECLARED packages, so an undeclared runtime import surfaces
     # when the app fails to start. The system still names the exact missing module.
     dict(success=False, final="failed", stage_in=("import", "start"), error_type="ImportError", error_package="yaml", llm_calls=1), None),
    ("wrong health path on a good app", dict(branch="eval-clean", instance_id=U, health_path="/nope"),
     dict(success=False, final="failed", stage_in=("health",), llm_calls=1), None),
    ("bad repo URL: unrecoverable, no fix ladder", dict(repo_url="https://github.com/SibgathKhan777/does-not-exist.git", instance_id=U),
     dict(success=False, final="failed", error_type="GitError", llm_calls=0, attempts=1), None),
    ("nonexistent target: graceful failure", dict(branch="eval-clean", instance_id="nomeshops-nope"),
     dict(success=False, final="failed", llm_calls=0, error_type="UnreachableTarget"), None),
]

results = []
for name, over, exp, setup in SCENARIOS:
    if setup: setup()
    req = {"repo_url": REPO, "instance_id": U, "branch": None, "app_port": 8000, "health_path": "/health",
           "keep_running": False, "preempt_predicted_fixes": True}
    req.update(over)
    t0 = time.time()
    try:
        st = run_deploy(req); s = summarize(st); crashed = None
    except Exception as e:  # noqa
        st, s, crashed = {}, {}, f"{type(e).__name__}: {e}"
    dur = round(time.time() - t0, 1)
    chain = [f["source"] for f in st.get("fix_chain", []) if f.get("signature")]
    llm = st.get("llm_attempts", 0)
    err = st.get("current_error") or {}
    checks = {}
    if crashed:
        checks["no_crash"] = (False, crashed)
    else:
        checks["success"] = (s.get("deploy_success") == exp["success"], s.get("deploy_success"))
        if "attempts" in exp: checks["attempts"] = (len(s.get("attempts", [])) == exp["attempts"], len(s.get("attempts", [])))
        if "fix_sources" in exp: checks["fix_sources"] = (chain == exp["fix_sources"], chain)
        if "llm_calls" in exp: checks["llm_calls"] = (llm == exp["llm_calls"], llm)
        if "final" in exp: checks["final_status"] = (s.get("final_status") == exp["final"], s.get("final_status"))
        if "stage_in" in exp: checks["failure_stage"] = (err.get("stage") in exp["stage_in"], err.get("stage"))
        if "error_type" in exp: checks["error_type"] = (err.get("error_type") == exp["error_type"], err.get("error_type"))
        if "error_package" in exp: checks["error_package"] = (err.get("package") == exp["error_package"], err.get("package"))
        if "predicted_rules" in exp:
            got = [i["rule"] for i in s.get("predicted_issues", [])]
            checks["predicted_rules"] = (all(r in got for r in exp["predicted_rules"]), got)
        if "start_contains" in exp: checks["start_command"] = (exp["start_contains"] in st.get("start_command", ""), st.get("start_command"))
    ok = all(v[0] for v in checks.values())
    results.append({"scenario": name, "pass": ok, "duration_s": dur, "checks": checks,
                    "failure_reason": s.get("failure_reason"), "events_tail": [e["msg"][:160] for e in s.get("events", [])[-4:]]})
    print(("PASS" if ok else "FAIL"), f"{dur:6.1f}s", name, "" if ok else {k: v for k, v in checks.items() if not v[0]}, flush=True)

os.makedirs("evals/results", exist_ok=True)
json.dump(results, open("evals/results/e2e_local.json", "w"), indent=2, default=str)
print(f"\n{sum(r['pass'] for r in results)}/{len(results)} scenarios passed")
