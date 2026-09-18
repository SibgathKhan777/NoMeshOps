"""Orchestration-level evaluation with the offline fakes (no network). Each scenario scripts how the fake target
responds to successive deploy attempts and states what a correct orchestrator must do."""
import json, os, shutil, sys
sys.path.insert(0, "."); sys.path.insert(0, "tests")
os.environ.update({"KB_BACKEND": "local", "LOCAL_DATA_DIR": ".nomeshops-graph-eval"})
import pytest  # noqa
from conftest import FakeBox, FakeKB, FakeBedrock  # type: ignore
from app.aws.ssm import CommandResult
from app.nodes.scan import ProjectScan, Dependency
from app import graph as G
from app.nodes import knowledge as kb_mod
from app.config import settings

def scan_stub(repo_url, branch=None):
    return ProjectScan(repo_url=repo_url, branch=branch, ok=True, has_requirements=True, framework="fastapi",
                       dependencies=[Dependency("fastapi", ">=0.100", "fastapi>=0.100"), Dependency("psycopg2", "==2.9.9", "psycopg2==2.9.9")],
                       entrypoint_candidates=["app.py"], file_count=3)

class ScriptedBox(FakeBox):
    """deploy_script: list of outcomes per deploy attempt: 'ok' | error text; verify_script: list per verify call."""
    def __init__(self, deploy_script, verify_script=("ok",)):
        super().__init__(); self.dep = list(deploy_script); self.ver_script = list(verify_script); self.dep_calls = 0; self.ver_calls = 0; self.seen_fixes = []
    def run(self, script):
        self.scripts.append(script)
        if "__KERNEL_NAME__" in script: return CommandResult("fp", "Success", 0, self.fingerprint(), "", 0.1)
        if "__STAGE__=import" in script:
            self.ver_calls += 1; o = self.ver_script[min(self.ver_calls - 1, len(self.ver_script) - 1)]
            if o == "ok": return CommandResult("v", "Success", 0, '__IMPORT__=OK\n__IMPORT_DETAIL__={"missing":[],"import_fail":[],"imported":["fastapi"]}\n__APP_PID__=1\n__HEALTH_STATUS__=200\n__HEALTH_BODY__=ok\n__HEALTH__=OK\n__RESULT__=verified\n', "", 0.5)
            if o == "import_fail": return CommandResult("v", "Failed", 21, '__IMPORT__=FAIL\n__IMPORT_DETAIL__={"missing":[],"import_fail":["psycopg2: ImportError: libpq.so.5: cannot open shared object file"],"imported":[]}\n__APP_PID__=1\n__HEALTH__=FAIL\n__RESULT__=health_failed\n', "", 0.5)
            if o == "health_fail": return CommandResult("v", "Failed", 21, '__IMPORT__=OK\n__IMPORT_DETAIL__={"missing":[],"import_fail":[],"imported":["fastapi"]}\n__APP_PID__=1\n__APP_EXITED__=1\n__HEALTH__=FAIL\n__APP_LOG_BEGIN__\nRuntimeError: DATABASE_URL is not set\n__APP_LOG_END__\n__RESULT__=health_failed\n', "", 0.5)
        if "__LOCK_OWNER__" in script: pass
        self.dep_calls += 1
        fixes = [l for l in script.splitlines() if l.startswith("set -e")]  # marker that a fix block exists
        self.seen_fixes.append("__NOMESHOPS_FIX__" in script)
        o = self.dep[min(self.dep_calls - 1, len(self.dep) - 1)]
        if o == "ok": return CommandResult("d", "Success", 0, "__STAGE__=install\n__RESULT__=install_ok\n", "", 1.0)
        return CommandResult("d", "Failed", 12, f"__STAGE__=install\n{o}\n__RESULT__=install_failed\n", "", 1.0)

PG = "Error: pg_config executable not found.\nERROR: Failed building wheel for psycopg2"
TYPO = "ERROR: No matching distribution found for reqests==2.31.0"
NUMPY = "RuntimeError: Running cythonize failed!\nERROR: Failed building wheel for numpy"

SCEN = [
 # name, deploy outcomes, verify outcomes, seeded kb items (list of (sig-from-error-text, fix, source)), bedrock fix, expectations
 ("rules fix resolves on retry", [PG, "ok"], ["ok"], [], None,
  dict(success=True, sources=["rules"], llm=0, stored=1)),
 ("rules fix does not help -> KB -> Bedrock, then success", [PG, PG, "ok"], ["ok"], [], "apt-get install -y build-essential",
  dict(success=True, sources=["rules", "bedrock"], llm=1, stored=1)),
 ("fix changes the error: new signature gets its own ladder", [TYPO, PG, "ok"], ["ok"], [], "sed -i s/x/y/ requirements.txt",
  dict(success=True, sources=["bedrock", "rules"], llm=1, max_attempts=3)),
 ("(limitation) second novel error cannot get LLM help: MAX_LLM_ATTEMPTS is per run", [TYPO, NUMPY, "ok"], ["ok"], [], "sed -i s/x/y/ requirements.txt",
  dict(success=False, llm=1, attempts=2)),
 ("verify import failure after install ok -> fix ladder -> success", ["ok", "ok"], ["import_fail", "ok"], [], "apt-get install -y libpq5",
  dict(success=True, sources=["bedrock"], llm=1, stage_seen="import")),
 ("app crashes at start, no fix known, LLM proposes nothing useful", ["ok", "ok"], ["health_fail", "health_fail"], [], "export DATABASE_URL=x",
  dict(success=False, llm=1, stored=0, attempts=2)),
 ("KB exact hit fails verification -> failure_count, then Bedrock", [TYPO, TYPO, "ok"], ["ok"], [("exact", "bogus fix", "seed")], "sed -i s/reqests/requests/ requirements.txt",
  dict(success=True, sources=["knowledge-exact", "bedrock"], llm=1, kb_failure_recorded=True)),
 ("stop condition: MAX_FIX_ATTEMPTS bounds a never-ending loop", [TYPO, TYPO, TYPO, TYPO, TYPO, TYPO], ["ok"], [], "echo nothing",
  dict(success=False, max_attempts=settings.max_fix_attempts + 1, llm_max=settings.max_llm_attempts)),
 ("unfixable error type: no ladder for bad repo", ["fatal: repository 'https://x/y.git/' not found"], ["ok"], [], None,
  dict(success=False, llm=0, attempts=1)),
 ("git 'not found' (binary missing) IS fixable via rules", ["sh: 1: git: not found", "ok"], ["ok"], [], None,
  dict(success=True, sources=["rules"], llm=0)),
 ("LLM proposes a destructive command -> rejected, clean failure", [TYPO, TYPO], ["ok"], [], "rm -rf / && pip install requests",
  dict(success=False, llm=1, stored=0, dangerous_never_ran=True)),
]

results = []
for name, dep, ver, seeds, brfix, exp in SCEN:
    shutil.rmtree(".nomeshops-graph-eval", ignore_errors=True)
    from app.local import kb_local
    box = ScriptedBox(dep, ver); kb = kb_local; br = FakeBedrock(fix_command=brfix or "true")
    if brfix and "rm -rf /" in brfix:
        # emulate the real filter: propose_fix raises before returning
        from app.nodes.bedrock_fix import DENY
        def propose(*a, **k):
            br.calls += 1
            if DENY.search(brfix): raise ValueError("blocked by safety filter")
            return br.propose(*a, **k)
    else:
        propose = br.propose
    G.scan_repo = scan_stub; G.propose_fix = propose; G.store_attempt = lambda rec: "attempts/x.json"
    import app.nodes.fingerprint as F, app.nodes.deploy as D, app.nodes.verify as V
    F.run_shell = D.run_shell = V.run_shell = lambda iid, script, **kw: box.run(script)
    G.knowledge = kb_mod  # dispatches to the real local backend via KB_BACKEND=local
    # seed: compute the signature the first failure will have
    if seeds:
        from app.nodes import signature
        from app.nodes.fingerprint import parse_fingerprint
        fpd = parse_fingerprint("i", box.fingerprint()).to_dict()
        e = signature.compute_signature(signature.classify(f"__STAGE__=install\n{dep[0]}\n", "", "install"), fpd)
        for kind, fix, src in seeds:
            kb.put_fix(e.to_dict(), fpd, fix, "seeded", src)
    req = {"repo_url": "https://github.com/x/y.git", "instance_id": "i-1", "app_port": 8000, "health_path": "/health", "preempt_predicted_fixes": False}
    try:
        st = G.run_deploy(req); crash = None
    except Exception as ex:  # noqa
        st, crash = {}, f"{type(ex).__name__}: {ex}"
    chain = [f["source"] for f in st.get("fix_chain", []) if f.get("signature")]
    checks = {}
    if crash: checks["no_crash"] = (False, crash)
    else:
        checks["success"] = (bool(st.get("deploy_success")) == exp["success"], st.get("deploy_success"))
        if "sources" in exp:
            want = exp["sources"]; got = chain
            ok = len(got) == len(want) and all(w == "?" or w == g for w, g in zip(want, got))
            checks["fix_sources"] = (ok, got)
        if "llm" in exp: checks["llm_calls"] = (br.calls == exp["llm"], br.calls)
        if "llm_max" in exp: checks["llm_calls_bounded"] = (br.calls <= exp["llm_max"], br.calls)
        if "stored" in exp:
            n_new = len(kb.list_fixes(500)) - len(seeds)
            checks["kb_stored"] = (n_new == exp["stored"], n_new)
        if "attempts" in exp: checks["attempts"] = (len(st.get("attempts", [])) == exp["attempts"], len(st.get("attempts", [])))
        if "max_attempts" in exp: checks["attempts_bounded"] = (len(st.get("attempts", [])) <= exp["max_attempts"], len(st.get("attempts", [])))
        if "stage_seen" in exp: checks["stage_seen"] = (any(a.get("error", {}).get("stage") == exp["stage_seen"] for a in st.get("attempts", [])), [a.get("error", {}).get("stage") for a in st.get("attempts", [])])
        if exp.get("kb_failure_recorded"):
            rows_ = kb.list_fixes(500)
            checks["kb_failure_recorded"] = (any(int(i.get("failure_count", 0)) >= 1 for i in rows_),
                                             [(i.get("failure_count"), i.get("superseded_fix", "")[:20]) for i in rows_])
        if exp.get("dangerous_never_ran"): checks["dangerous_never_ran"] = (not any("rm -rf /" in s for s in box.scripts), None)
    ok = all(v[0] for v in checks.values())
    results.append({"scenario": name, "pass": ok, "checks": checks, "failure_reason": st.get("failure_reason")})
    print(("PASS" if ok else "FAIL"), name, "" if ok else {k: v for k, v in checks.items() if not v[0]}, "|", (st.get("failure_reason") or "")[:90])
os.makedirs("evals/results", exist_ok=True)
json.dump(results, open("evals/results/graph_scenarios.json", "w"), indent=2, default=str)
print(f"\n{sum(r['pass'] for r in results)}/{len(results)} scenarios passed")
