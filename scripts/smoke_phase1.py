"""Phase 1 smoke test: fingerprint + deploy attempt + error capture against ONE real EC2 instance.
Asserts a populated fingerprint and a structured deploy_success boolean. No exception, no hang.
Usage: python scripts/smoke_phase1.py --instance i-... --repo https://github.com/you/sample [--url http://orchestrator]
"""
import argparse, json, sys, time
sys.path.insert(0, ".")

p = argparse.ArgumentParser()
p.add_argument("--instance", required=True); p.add_argument("--repo", required=True); p.add_argument("--url")
a = p.parse_args()
req = {"repo_url": a.repo, "instance_id": a.instance, "app_port": 8000, "health_path": "/health", "preempt_predicted_fixes": True}
t0 = time.time()
if a.url:
    import httpx
    s = httpx.post(f"{a.url.rstrip('/')}/deploy", json=req, timeout=None).json()
else:
    from app.graph import run_deploy, summarize
    s = summarize(run_deploy(req))
fp = s.get("fingerprint") or {}
for k in ("os", "arch", "python_version"):
    assert fp.get(k) not in (None, "", "unknown"), f"fingerprint.{k} not populated: {fp}"
assert isinstance(s.get("deploy_success"), bool), f"deploy_success is not a bool: {s.get('deploy_success')!r}"
assert s["attempts"], "no deploy attempts recorded"
print(json.dumps({"final_status": s["final_status"], "deploy_success": s["deploy_success"], "fingerprint": {k: fp[k] for k in ("os", "os_version", "arch", "python_version")},
                  "attempts": len(s["attempts"]), "duration_s": round(time.time() - t0, 1)}, indent=2))
print("PHASE 1 SMOKE: PASS")
