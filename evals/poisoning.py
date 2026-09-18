"""Does a TRANSIENT failure teach the system a false lesson?
A network blip is not a code defect. If the retry succeeds merely because the network recovered, any fix
applied in between gets credited and stored, and is then replayed on every future run with that signature."""
import json, os, shutil, sys
sys.path.insert(0, "."); sys.path.insert(0, "tests")
os.environ.update({"KB_BACKEND": "local", "LOCAL_DATA_DIR": ".nomeshops-poison-eval"})
shutil.rmtree(".nomeshops-poison-eval", ignore_errors=True)
from conftest import FakeBox, FakeBedrock  # type: ignore
from app.aws.ssm import CommandResult
from app.nodes.scan import ProjectScan, Dependency
from app import graph as G
from app.nodes import knowledge, signature
from app.local import kb_local

NET = """WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken by 'ReadTimeoutError("HTTPSConnectionPool(host='pypi.org', port=443): Read timed out. (read timeout=15)")': /simple/fastapi/
ERROR: Could not find a version that satisfies the requirement fastapi>=0.110 (from versions: none)
ERROR: No matching distribution found for fastapi>=0.110"""

class FlakyBox(FakeBox):
    """Fails the first deploy with a network blip, then succeeds regardless of what fix was applied."""
    def __init__(self): super().__init__(); self.n = 0
    def run(self, script):
        self.scripts.append(script)
        if "__KERNEL_NAME__" in script: return CommandResult("fp","Success",0,self.fingerprint(),"",0.1)
        if "__STAGE__=import" in script:
            return CommandResult("v","Success",0,'__IMPORT__=OK\n__IMPORT_DETAIL__={"missing":[],"import_fail":[],"imported":["fastapi"]}\n__APP_PID__=1\n__PORT_OWNER__=1\n__HEALTH_STATUS__=200\n__HEALTH_BODY__=ok\n__HEALTH__=OK\n__RESULT__=verified\n',"",0.3)
        self.n += 1
        if self.n == 1:
            return CommandResult("d","Failed",12,f"__STAGE__=install\n{NET}\n__RESULT__=install_failed\n","",1.0)
        return CommandResult("d","Success",0,"__STAGE__=install\n__RESULT__=install_ok\n","",1.0)

def scan_stub(repo_url, branch=None):
    return ProjectScan(repo_url=repo_url, branch=branch, ok=True, has_requirements=True, framework="fastapi",
                       dependencies=[Dependency("fastapi", ">=0.110", "fastapi>=0.110")], entrypoint_candidates=["app.py"])

box = FlakyBox(); br = FakeBedrock(fix_command="pip install --upgrade pip setuptools wheel")
G.scan_repo = scan_stub; G.propose_fix = br.propose; G.store_attempt = lambda rec: "x"
import app.nodes.fingerprint as F, app.nodes.deploy as D, app.nodes.verify as V
F.run_shell = D.run_shell = V.run_shell = lambda iid, script, **kw: box.run(script)
G.knowledge = knowledge

st = G.run_deploy({"repo_url":"https://github.com/x/y.git","instance_id":"i-1","app_port":8000,"health_path":"/health","preempt_predicted_fixes":False})
err = [a.get("error") for a in st["attempts"] if a.get("error")][0]
stored = kb_local.list_fixes(50)
print("transient failure classified as:", err["error_type"], "| package:", err["package"])
print("run succeeded:", st["deploy_success"], "| LLM calls:", st.get("llm_attempts"))
print("rows written to the knowledge base:", len(stored))
for r in stored:
    print("  STORED:", json.dumps({k: r[k] for k in ("error_signature","error_type","package","fix_command","source","success_count")}))
print()
print("VERDICT:", "POISONED - a network blip is now a permanent 'verified fix'" if stored else "clean - nothing learned from a transient failure")
json.dump({"classified_as": err["error_type"], "package": err.get("package"), "succeeded": st["deploy_success"],
           "rows_stored": len(stored), "rows": stored}, open("evals/results/poisoning.json","w"), indent=2, default=str)
