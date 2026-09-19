"""FastAPI orchestrator. Deployed on ECS Express Mode; also runs locally with uvicorn."""
from __future__ import annotations

import json

import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.graph import run_deploy, stream_deploy, summarize
from app.models import DeployRequest, DeployResponse, SeedFixRequest
from app.nodes import knowledge, rules
from app.nodes.fingerprint import fingerprint_instance
from app.nodes.store import list_attempts

app = FastAPI(title="NoMeshOps", version="0.1.0",
              description="Self-healing deployment agent: fingerprint -> deploy -> rules -> knowledge base -> Bedrock -> verify -> remember")

from app.web import router as web_router  # noqa: E402

_STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
app.include_router(web_router)


@app.get("/", include_in_schema=False)
def _home():
    return FileResponse(os.path.join(_STATIC, "index.html"))


@app.get("/demo", include_in_schema=False)
def _demo():
    return FileResponse(os.path.join(_STATIC, "demo.html"))


@app.get("/device", include_in_schema=False)
def _device():
    return FileResponse(os.path.join(_STATIC, "device.html"))


@app.get("/health")
def health():
    return {"ok": True, "region": settings.aws_region, "fixes_table": settings.fixes_table,
            "logs_bucket": settings.logs_bucket or None, "model": settings.bedrock_model_id}


@app.post("/deploy", response_model=DeployResponse)
def deploy(req: DeployRequest):
    state = run_deploy(req.model_dump())
    return summarize(state)


@app.post("/deploy/stream")
def deploy_stream(req: DeployRequest):
    """Server-sent events: one `event` line per orchestrator event, then a final `result` event."""
    def gen():
        final = None
        for node, delta in stream_deploy(req.model_dump()):
            for ev in delta.get("events", []) or []:
                yield f"event: log\ndata: {json.dumps(ev)}\n\n"
            if node == "finalize":
                final = delta
        yield f"event: result\ndata: {json.dumps({'final_status': (final or {}).get('final_status', 'error'), 'deploy_success': bool((final or {}).get('deploy_success')), 'failure_reason': (final or {}).get('failure_reason'), 's3_key': (final or {}).get('s3_key'), 'stored_fix': bool((final or {}).get('stored_fix')), 'duration_s': (final or {}).get('duration_s')})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/fingerprint/{instance_id}")
def fingerprint(instance_id: str):
    fp, raw = fingerprint_instance(instance_id)
    return {"fingerprint": fp.to_dict(), "ssm": {k: v for k, v in raw.items() if k != "stdout"}}


@app.post("/check")
def check(payload: dict):
    """Offline deterministic check: {"scan": {...}, "fingerprint": {...}} -> predicted issues. No AWS calls."""
    return {"issues": [i.to_dict() for i in rules.check_compatibility(payload.get("scan", {}), payload.get("fingerprint", {}))]}


@app.get("/fixes")
def fixes(limit: int = 50):
    return {"items": knowledge.list_fixes(limit)}


@app.post("/fixes/seed")
def seed_fix(req: SeedFixRequest):
    error = {"signature": req.error_signature, "family": req.error_family, "error_type": req.error_type,
             "package": req.package, "package_version": req.package_version}
    fp = {"os_key": req.os, "arch": req.architecture, "python_minor": req.python_version}
    return knowledge.put_fix(error, fp, req.fix_command, req.description, "seed")


@app.delete("/fixes/{error_signature}")
def delete_fix(error_signature: str):
    knowledge.delete_fix(error_signature)
    return {"deleted": error_signature}


@app.get("/attempts")
def attempts(limit: int = 20):
    if not settings.logs_bucket:
        raise HTTPException(400, "LOGS_BUCKET is not configured")
    return {"items": list_attempts(limit)}
