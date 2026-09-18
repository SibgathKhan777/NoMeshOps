"""Phase 3 smoke test: the demo beat.
 1. On instance A, trigger a novel error (signature not in DynamoDB): assert Bedrock is called, the fix verifies
    (import + start + health), the fix lands in DynamoDB and the attempt log lands in S3.
 2. On instance B (same OS/arch/python), re-trigger: assert knowledge-base path, no Bedrock call, faster.
Usage: python scripts/smoke_phase3.py --repo URL --instance-a i-... --instance-b i-...
"""
import argparse, sys, time
sys.path.insert(0, ".")
from app.graph import run_deploy, summarize
from app.nodes import knowledge

p = argparse.ArgumentParser()
p.add_argument("--repo", required=True); p.add_argument("--instance-a", required=True); p.add_argument("--instance-b", required=True)
a = p.parse_args()

def run(instance):
    t0 = time.time()
    s = summarize(run_deploy({"repo_url": a.repo, "instance_id": instance, "app_port": 8000, "health_path": "/health", "preempt_predicted_fixes": True}))
    return s, time.time() - t0

# make sure the error is novel: wipe any fix for the signature instance A will produce (probe once)
probe, _ = run(a.instance_a)
for att in probe["attempts"]:
    if att.get("error"):
        knowledge.delete_fix(att["error"]["signature"])
print("cleared knowledge base entries for this error; running the real thing")

s1, t1 = run(a.instance_a)
llm = [e for e in s1["events"] if e["node"] == "bedrock" and "proposed" in e["msg"]]
assert llm, "Bedrock was not called for a novel error"
assert s1["deploy_success"], f"instance A did not verify: {s1['failure_reason']}"
v = s1["attempts"][-1]["verify"]
assert v["import_ok"] and v["app_started"] and v["health_ok"], v
assert s1["stored_fix"], "verified fix was not stored"
assert s1["s3_key"], "attempt log not written to S3"
print(f"step 1: Bedrock fix verified + stored (sig {s1['fix_applied']['signature']}) in {t1:.1f}s -> PASS")

s2, t2 = run(a.instance_b)
assert s2["deploy_success"], f"instance B did not verify: {s2['failure_reason']}"
assert s2["fix_applied"] and s2["fix_applied"]["source"].startswith("knowledge"), s2["fix_applied"]
assert not [e for e in s2["events"] if e["node"] == "bedrock"], "Bedrock was called on the second instance"
print(f"step 2: knowledge-base hit on instance B in {t2:.1f}s (vs {t1:.1f}s) with zero LLM calls -> PASS")
print("PHASE 3 SMOKE: PASS")
