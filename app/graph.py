"""The LangGraph orchestrator. One deployed service; the "agents" from the brainstorm are nodes:

  scan_project -> fingerprint_target -> deterministic_check -> deploy -> verify -> finalize
                                                                 |         |
                                                                 v         v
                                          classify failure -> lookup_rules -> lookup_knowledge -> ask_bedrock
                                                                 ^______________ apply fix, retry ____|

Stop conditions: MAX_FIX_ATTEMPTS total fix retries, MAX_LLM_ATTEMPTS Bedrock calls. No indefinite looping.
"""
from __future__ import annotations

import operator
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from app.config import settings
from app.nodes import knowledge, rules, signature
from app.nodes.bedrock_fix import propose_fix
from app.nodes.deploy import deploy_attempt
from app.nodes.fingerprint import fingerprint_instance
from app.nodes.scan import guess_start_command, scan_repo
from app.nodes.store import store_attempt
from app.nodes.verify import verify_deploy


class DeployState(TypedDict, total=False):
    request: dict
    run_id: str
    started_at: float
    scan: dict
    fingerprint: dict
    predicted_issues: list[dict]
    start_command: str
    attempts: list[dict]
    events: Annotated[list[dict], operator.add]
    fix_chain: list[dict]            # fixes applied so far this run, in order: {signature, source, fix_command, stage, ...}
    current_fix: dict | None
    current_error: dict | None
    last_output: str
    fix_attempts: int
    llm_attempts: int
    tried: dict[str, list[str]]      # signature -> sources already consulted
    deploy_success: bool
    final_status: str
    failure_reason: str
    stored_fix: bool
    s3_key: str | None
    duration_s: float


def _ev(state: DeployState, level: str, node: str, msg: str, **data: Any) -> dict:
    return {"t": round(time.time() - state.get("started_at", time.time()), 2), "level": level,
            "node": node, "msg": msg, **data}


# --------------------------------------------------------------------------- nodes

def scan_project(state: DeployState) -> dict:
    req = state["request"]
    scan = scan_repo(req["repo_url"], req.get("branch")).to_dict()
    events = []
    if scan["ok"]:
        deps = ", ".join(d["name"] + d["specifier"] for d in scan["dependencies"][:12])
        events.append(_ev(state, "info", "scan", f"scanned {req['repo_url']}: {len(scan['dependencies'])} deps "
                          f"[{deps}{'…' if len(scan['dependencies']) > 12 else ''}]"
                          f"{' requires-python ' + scan['python_requires'] if scan['python_requires'] else ''}"
                          f"{' framework=' + scan['framework'] if scan['framework'] else ''}"
                          f"{' Dockerfile' if scan['has_dockerfile'] else ''}"))
    else:
        events.append(_ev(state, "warn", "scan", f"local scan failed ({scan['error'][:120]}); continuing without manifest"))
    start = req.get("start_command") or guess_start_command(scan, req.get("app_port", 8000))
    return {"scan": scan, "start_command": start, "events": events}


def fingerprint_target(state: DeployState) -> dict:
    iid = state["request"]["instance_id"]
    fp, raw = fingerprint_instance(iid)
    d = fp.to_dict()
    events = [_ev(state, "info", "fingerprint",
                  f"{iid}: {d['os_pretty'] or d['os']} {d['arch']} python={d['python_version'] or 'MISSING'} "
                  f"pip={'yes' if d['has_pip'] else 'no'} venv={'yes' if d['has_venv'] else 'no'} "
                  f"git={'yes' if d['has_git'] else 'no'} gcc={'yes' if d['has_gcc'] else 'no'} "
                  f"pkg={d['package_manager'] or '?'} ({raw['duration_s']}s via {'docker' if settings.executor == 'docker' else 'SSM'})")]
    if raw["status"] != "Success":
        events.append(_ev(state, "error", "fingerprint", f"SSM {raw['status']}: {raw['stderr'][:200]}"))
    return {"fingerprint": d, "events": events}


def deterministic_check(state: DeployState) -> dict:
    t0 = time.perf_counter()
    issues = [i.to_dict() for i in rules.check_compatibility(state["scan"], state["fingerprint"])]
    ms = (time.perf_counter() - t0) * 1000
    events = [_ev(state, "rules", "check", f"deterministic check: {len(issues)} predicted issue(s) in {ms:.1f} ms")]
    for i in issues:
        events.append(_ev(state, "rules", "check", f"  [{i['severity']}] {i['rule']}: {i['message']}"
                          + (f" -> fix: {i['fix_command']}" if i["fix_command"] else "")))
    preempt = state["request"].get("preempt_predicted_fixes")
    if preempt is None:
        preempt = settings.preempt_predicted_fixes
    chain: list[dict] = []
    if preempt:
        for i in issues:
            if i["fix_command"] and i["severity"] in ("blocker", "warning"):
                chain.append({"signature": "", "source": "rules-predicted", "rule": i["rule"],
                              "fix_command": i["fix_command"], "stage": i.get("stage", "pre"),
                              "description": i["fix_description"]})
        if chain:
            events.append(_ev(state, "rules", "check", f"pre-applying {len(chain)} deterministic fix(es) before first deploy"))
    return {"predicted_issues": issues, "fix_chain": chain, "events": events,
            "attempts": [], "fix_attempts": 0, "llm_attempts": 0, "tried": {}, "current_fix": None}


def deploy(state: DeployState) -> dict:
    req = state["request"]
    chain = list(state.get("fix_chain", []))
    fix = state.get("current_fix")
    events = []
    if fix:
        chain.append(fix)
        events.append(_ev(state, fix["source"].split("-")[0], "deploy",
                          f"applying fix from {fix['source']} ({fix.get('stage','post')}): {fix['fix_command']}"))
    pre = [f["fix_command"] for f in chain if f.get("stage", "post") == "pre"]
    post = [f["fix_command"] for f in chain if f.get("stage", "post") != "pre"]
    n = len(state.get("attempts", [])) + 1
    events.append(_ev(state, "info", "deploy", f"attempt {n}: clone + venv + install on {req['instance_id']}"))
    res = deploy_attempt(req["repo_url"], req.get("branch"), req["instance_id"], state["run_id"], pre, post)
    fingerprint = state["fingerprint"]
    if pre and n == 1:
        # pre-stage fixes may have installed python/git: refresh the fingerprint so signatures carry the real runtime
        try:
            fp2, _ = fingerprint_instance(req["instance_id"])
            fingerprint = fp2.to_dict()
            events.append(_ev(state, "info", "fingerprint",
                              f"re-fingerprinted after bootstrap: python={fingerprint['python_version'] or 'MISSING'} "
                              f"git={'yes' if fingerprint['has_git'] else 'no'} pip={'yes' if fingerprint['has_pip'] else 'no'}"))
        except Exception as e:  # noqa: BLE001
            events.append(_ev(state, "warn", "fingerprint", f"re-fingerprint failed: {type(e).__name__}: {str(e)[:120]}"))
    attempt = {"n": n, "fix": fix, "stage": "install", "result": res["result"], "ok": res["ok"],
               "duration_s": res["duration_s"], "command_id": res["command_id"], "exit_code": res["exit_code"],
               "output_tail": (res["stdout"] + "\n" + res["stderr"])[-3000:]}
    attempts = list(state.get("attempts", [])) + [attempt]
    out = {"attempts": attempts, "fix_chain": chain, "current_fix": None, "fingerprint": fingerprint,
           "fix_attempts": state.get("fix_attempts", 0) + (1 if fix else 0), "events": events}
    if res["ok"]:
        events.append(_ev(state, "success", "deploy", f"install ok in {res['duration_s']}s"))
        out["current_error"] = None
        return out
    output = res["stdout"] + "\n" + res["stderr"]
    err = signature.classify(res["stdout"], res["stderr"], res.get("failed_stage") or "install",
                             state["scan"].get("dependencies"))
    err = signature.compute_signature(err, fingerprint).to_dict()
    events.append(_ev(state, "error", "deploy",
                      f"attempt {n} failed at {err['stage']} ({res['result']}, {res['duration_s']}s): "
                      f"{err['error_type']} {err['package'] + err['package_version'] + ' ' if err['package'] else ''}"
                      f"— {err['message'][:160]}"))
    events.append(_ev(state, "info", "signature", f"error signature {err['signature']} (family {err['family']})"))
    attempts[-1]["error"] = err
    out["current_error"] = err
    out["last_output"] = output
    return out


def verify(state: DeployState) -> dict:
    req = state["request"]
    deps = [d["name"] for d in state["scan"].get("dependencies", [])]
    events = [_ev(state, "info", "verify", f"verifying: import check -> start `{state['start_command']}` -> "
                                            f"GET :{req.get('app_port', 8000)}{req.get('health_path', '/health')}")]
    res = verify_deploy(req["repo_url"], req["instance_id"], state["run_id"], deps, state["start_command"],
                        req.get("app_port", 8000), req.get("health_path", "/health"), req.get("keep_running", False))
    attempts = list(state["attempts"])
    attempts[-1] = {**attempts[-1], "verify": {k: v for k, v in res.items() if k not in ("stdout", "stderr")},
                    "ok": res["ok"], "stage": res["stage"], "result": res["result"],
                    "verify_output_tail": (res["stdout"] + "\n" + res["stderr"])[-3000:]}
    out: dict = {"attempts": attempts, "events": events}
    det = res.get("import_detail", {})
    if res["ok"]:
        events.append(_ev(state, "success", "verify",
                          f"import ok ({len(det.get('imported', []))} packages) · app started · "
                          f"health {res['health_status']} {str(res['health_body'])[:80]!r} ({res['duration_s']}s)"))
        out["deploy_success"] = True
        out["current_error"] = None
        return out
    stdout = res["stdout"]
    if not res["import_ok"]:
        msg = "; ".join(det.get("missing", []) + det.get("import_fail", []))[:300]
        events.append(_ev(state, "error", "verify", f"import check FAILED: {msg}"))
        stdout = stdout + "\n" + "\n".join(det.get("import_fail", []))
    elif not res["app_started"]:
        events.append(_ev(state, "error", "verify", "app process exited before answering the health check"))
    else:
        events.append(_ev(state, "error", "verify", f"health check FAILED: {res.get('health_error') or res.get('health_status')}"))
    err = signature.classify(stdout, res["stderr"], res["stage"], state["scan"].get("dependencies"))
    err = signature.compute_signature(err, state["fingerprint"]).to_dict()
    events.append(_ev(state, "info", "signature", f"error signature {err['signature']} (family {err['family']})"))
    attempts[-1]["error"] = err
    out["current_error"] = err
    out["last_output"] = stdout + "\n" + res["stderr"]
    out["deploy_success"] = False
    return out


def _mark(state: DeployState, source: str) -> dict:
    sig = state["current_error"]["signature"]
    tried = {k: list(v) for k, v in state.get("tried", {}).items()}
    tried.setdefault(sig, []).append(source)
    return tried


def lookup_rules(state: DeployState) -> dict:
    err = state["current_error"]
    hit = rules.match_error_rule(state.get("last_output", ""), state["fingerprint"])
    tried = _mark(state, "rules")
    if hit:
        fix = {"signature": err["signature"], "source": "rules", "rule": hit.rule, "fix_command": hit.fix_command,
               "stage": hit.stage, "description": hit.description}
        return {"tried": tried, "current_fix": fix,
                "events": [_ev(state, "rules", "rules", f"rules table HIT ({hit.rule}): {hit.description}")]}
    return {"tried": tried, "current_fix": None,
            "events": [_ev(state, "rules", "rules", "rules table: no deterministic fix for this error")]}


def lookup_knowledge(state: DeployState) -> dict:
    err = state["current_error"]
    tried = _mark(state, "knowledge")
    t0 = time.perf_counter()
    item = knowledge.get_fix(err["signature"])
    ms = (time.perf_counter() - t0) * 1000
    if item:
        fix = {"signature": err["signature"], "source": "knowledge-exact", "fix_command": item["fix_command"],
               "stage": item.get("stage", "post"), "description": item.get("description", ""),
               "success_count": item.get("success_count", 0)}
        return {"tried": tried, "current_fix": fix, "events": [_ev(
            state, "kb", "knowledge",
            f"knowledge base HIT in {ms:.0f} ms (verified {item.get('success_count', 0)}x, "
            f"first seen {str(item.get('first_seen_at', ''))[:19]}): {item.get('description') or item['fix_command']}")]}
    fam = knowledge.get_family_fixes(err["family"]) if err.get("family") else []
    fam = [f for f in fam if f["error_signature"] != err["signature"]]
    if fam:
        best = fam[0]
        fix = {"signature": err["signature"], "source": "knowledge-family", "fix_command": best["fix_command"],
               "stage": best.get("stage", "post"), "description": best.get("description", ""),
               "from_signature": best["error_signature"], "from_platform": f"{best.get('os')}/{best.get('python_version')}"}
        return {"tried": tried, "current_fix": fix, "events": [_ev(
            state, "kb", "knowledge",
            f"knowledge base: no exact match; FAMILY match from {fix['from_platform']} in {ms:.0f} ms "
            f"— trying it (verification is the gate): {best['fix_command']}")]}
    return {"tried": tried, "current_fix": None,
            "events": [_ev(state, "kb", "knowledge", f"knowledge base MISS in {ms:.0f} ms (signature {err['signature']})")]}


def ask_bedrock(state: DeployState) -> dict:
    err = state["current_error"]
    tried = _mark(state, "bedrock")
    hint = None
    for f in state.get("fix_chain", []):
        if f.get("source") == "knowledge-family" and f.get("signature") == err["signature"]:
            hint = {"os": f.get("from_platform"), "python_version": "", "fix_command": f["fix_command"]}
    events = [_ev(state, "llm", "bedrock", f"novel error -> asking {settings.llm_label} for one fix…")]
    t0 = time.perf_counter()
    try:
        prop = propose_fix(state["fingerprint"], state["scan"], err, state.get("last_output", ""),
                           state.get("attempts", []), hint)
    except Exception as e:  # noqa: BLE001
        events.append(_ev(state, "error", "bedrock", f"LLM call failed: {type(e).__name__}: {str(e)[:300]}"))
        return {"tried": tried, "current_fix": None, "llm_attempts": state.get("llm_attempts", 0) + 1, "events": events}
    secs = time.perf_counter() - t0
    fix = {"signature": err["signature"], **prop}
    events.append(_ev(state, "llm", "bedrock",
                      f"{'Bedrock' if settings.llm_backend == 'bedrock' else settings.llm_backend} proposed (confidence {prop['confidence']:.2f}, {secs:.1f}s, "
                      f"{prop.get('input_tokens')}/{prop.get('output_tokens')} tokens): {prop['description']}"))
    events.append(_ev(state, "llm", "bedrock", f"  rationale: {prop['rationale']}"))
    return {"tried": tried, "current_fix": fix, "llm_attempts": state.get("llm_attempts", 0) + 1, "events": events}


def finalize(state: DeployState) -> dict:
    events = []
    success = bool(state.get("deploy_success"))
    stored = False
    chain = state.get("fix_chain", [])
    fp = state["fingerprint"]
    reason = ""
    if success:
        for f in chain:
            src = f.get("source", "")
            if not f.get("signature"):
                continue  # rules-predicted fixes have no failure signature
            try:
                if src == "knowledge-exact":
                    knowledge.record_outcome(f["signature"], True)
                    events.append(_ev(state, "kb", "store", f"knowledge base: success_count +1 for {f['signature']}"))
                else:
                    err = _error_for_signature(state, f["signature"]) or dict(state.get("current_error") or {})
                    if not err.get("signature"):
                        err["signature"] = f["signature"]
                    knowledge.put_fix(err, fp, f["fix_command"], f.get("description", ""), src)
                    stored = True
                    events.append(_ev(state, "kb", "store",
                                      f"stored verified fix in {'local knowledge base' if settings.kb_backend == 'local' else 'DynamoDB'}: {f['signature']} <- {src} ({f['fix_command'][:80]})"))
            except Exception as e:  # noqa: BLE001
                events.append(_ev(state, "error", "store", f"knowledge base write failed: {type(e).__name__}: {str(e)[:200]}"))
        status = "success"
    else:
        for f in chain:
            if f.get("source") == "knowledge-exact":
                try:
                    knowledge.record_outcome(f["signature"], False)
                except Exception:  # noqa: BLE001
                    pass
        err = state.get("current_error") or {}
        fa, la = state.get("fix_attempts", 0), state.get("llm_attempts", 0)
        if fa >= settings.max_fix_attempts:
            reason = f"gave up after {fa} fix attempts (MAX_FIX_ATTEMPTS)"
        elif la >= settings.max_llm_attempts and err:
            reason = (f"no fix found: rules miss, knowledge miss, and {la} LLM attempt(s) did not verify"
                      if settings.llm_backend != "none" else "no fix found: rules miss, knowledge miss, LLM fallback disabled")
        else:
            reason = f"unrecoverable: {err.get('error_type', 'error')} at {err.get('stage', '?')}: {err.get('message', '')[:200]}"
        events.append(_ev(state, "error", "finalize", reason))
        status = "failed"
    duration = round(time.time() - state["started_at"], 2)
    record = {
        "run_id": state["run_id"], "instance_id": state["request"]["instance_id"], "request": state["request"],
        "started_at": datetime.fromtimestamp(state["started_at"], timezone.utc).isoformat(),
        "duration_s": duration, "final_status": status, "failure_reason": reason,
        "fingerprint": fp, "scan": state.get("scan"), "predicted_issues": state.get("predicted_issues", []),
        "attempts": state.get("attempts", []), "fix_chain": chain, "stored_fix": stored,
        "events": state.get("events", []) + events,
    }
    key = None
    try:
        key = store_attempt(record)
        if key:
            dest = key if settings.store_backend == "local" else f"s3://{settings.logs_bucket}/{key}"
            events.append(_ev(state, "info", "store", f"attempt log written to {dest}"))
    except Exception as e:  # noqa: BLE001
        events.append(_ev(state, "error", "store", f"S3 write failed: {type(e).__name__}: {str(e)[:200]}"))
    events.append(_ev(state, "success" if success else "error", "finalize",
                      f"DEPLOY {'VERIFIED' if success else 'FAILED'} in {duration}s "
                      f"({len(state.get('attempts', []))} attempt(s), {len([f for f in chain if f.get('signature')])} fix(es), "
                      f"{state.get('llm_attempts', 0)} LLM call(s))"))
    return {"final_status": status, "failure_reason": reason, "stored_fix": stored, "s3_key": key,
            "duration_s": duration, "deploy_success": success, "events": events}


def _error_for_signature(state: DeployState, sig: str) -> dict | None:
    # attempts carry the fix that was applied; the error it addressed is the failure of the preceding attempt
    attempts = state.get("attempts", [])
    for i, a in enumerate(attempts):
        if a.get("fix") and a["fix"].get("signature") == sig and i > 0:
            prev = attempts[i - 1]
            return prev.get("error")
    return None


# --------------------------------------------------------------------------- routing

def after_deploy(state: DeployState) -> str:
    return "verify" if state.get("current_error") is None else "route_failure"


def after_verify(state: DeployState) -> str:
    return "finalize" if state.get("deploy_success") else "route_failure"


def route_failure(state: DeployState) -> str:
    err = state.get("current_error") or {}
    if not err or state.get("fix_attempts", 0) >= settings.max_fix_attempts:
        return "finalize"
    if err.get("error_type") in ("GitError",) and "not found" not in err.get("message", "").lower():
        return "finalize"  # bad repo URL / branch: no fix will help
    tried = state.get("tried", {}).get(err["signature"], [])
    if "rules" not in tried:
        return "lookup_rules"
    if "knowledge" not in tried:
        return "lookup_knowledge"
    if "bedrock" not in tried and state.get("llm_attempts", 0) < settings.max_llm_attempts:
        return "ask_bedrock"
    return "finalize"


def after_lookup(state: DeployState) -> str:
    return "deploy" if state.get("current_fix") else route_failure(state)


def build_graph():
    g = StateGraph(DeployState)
    g.add_node("scan_project", scan_project)
    g.add_node("fingerprint_target", fingerprint_target)
    g.add_node("deterministic_check", deterministic_check)
    g.add_node("deploy", deploy)
    g.add_node("verify", verify)
    g.add_node("lookup_rules", lookup_rules)
    g.add_node("lookup_knowledge", lookup_knowledge)
    g.add_node("ask_bedrock", ask_bedrock)
    g.add_node("finalize", finalize)

    g.set_entry_point("scan_project")
    g.add_edge("scan_project", "fingerprint_target")
    g.add_edge("fingerprint_target", "deterministic_check")
    g.add_edge("deterministic_check", "deploy")
    targets = {"verify": "verify", "finalize": "finalize", "lookup_rules": "lookup_rules",
               "lookup_knowledge": "lookup_knowledge", "ask_bedrock": "ask_bedrock", "deploy": "deploy"}
    g.add_conditional_edges("deploy", lambda s: after_deploy(s) if after_deploy(s) == "verify" else route_failure(s), targets)
    g.add_conditional_edges("verify", lambda s: "finalize" if s.get("deploy_success") else route_failure(s), targets)
    g.add_conditional_edges("lookup_rules", after_lookup, targets)
    g.add_conditional_edges("lookup_knowledge", after_lookup, targets)
    g.add_conditional_edges("ask_bedrock", after_lookup, targets)
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


def initial_state(request: dict) -> DeployState:
    return {"request": request, "run_id": uuid.uuid4().hex[:8], "started_at": time.time(), "events": [],
            "attempts": [], "fix_chain": [], "tried": {}, "fix_attempts": 0, "llm_attempts": 0}


def run_deploy(request: dict) -> DeployState:
    return GRAPH.invoke(initial_state(request), config={"recursion_limit": 60})


def stream_deploy(request: dict):
    """Yield (node_name, update_dict) as each node finishes. The last update comes from `finalize`."""
    for update in GRAPH.stream(initial_state(request), config={"recursion_limit": 60}, stream_mode="updates"):
        for node, delta in update.items():
            yield node, delta


def summarize(state: DeployState) -> dict:
    chain = [f for f in state.get("fix_chain", []) if f.get("signature")]
    return {
        "run_id": state["run_id"], "final_status": state.get("final_status", "error"),
        "deploy_success": bool(state.get("deploy_success")), "failure_reason": state.get("failure_reason") or None,
        "fingerprint": state.get("fingerprint"), "scan": state.get("scan"),
        "predicted_issues": state.get("predicted_issues", []), "attempts": state.get("attempts", []),
        "fix_applied": chain[-1] if chain else None, "stored_fix": bool(state.get("stored_fix")),
        "s3_key": state.get("s3_key"), "events": state.get("events", []), "duration_s": state.get("duration_s", 0.0),
    }
