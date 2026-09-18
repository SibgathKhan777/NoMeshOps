"""Pydantic request/response models for the API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DeployRequest(BaseModel):
    repo_url: str = Field(..., description="Git URL of the project to deploy")
    instance_id: str = Field(..., description="Target EC2 instance id (SSM-managed)")
    branch: Optional[str] = Field(None, description="Branch/tag to clone (default: repo default)")
    start_command: Optional[str] = Field(
        None,
        description="Command that starts the app, run inside the project dir with the venv on PATH. "
        "Auto-detected for FastAPI/Flask if omitted.",
    )
    app_port: int = Field(8000, description="Port the app listens on for the health check")
    health_path: str = Field("/health", description="HTTP path polled for the health check")
    keep_running: bool = Field(False, description="Leave the app running after a verified deploy")
    preempt_predicted_fixes: Optional[bool] = Field(
        None, description="Override PREEMPT_PREDICTED_FIXES for this run"
    )


class DeployResponse(BaseModel):
    run_id: str
    final_status: str
    deploy_success: bool
    failure_reason: Optional[str] = None
    fingerprint: Optional[dict[str, Any]] = None
    scan: Optional[dict[str, Any]] = None
    predicted_issues: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    fix_applied: Optional[dict[str, Any]] = None
    stored_fix: bool = False
    s3_key: Optional[str] = None
    events: list[dict[str, Any]] = []
    duration_s: float = 0.0


class SeedFixRequest(BaseModel):
    error_signature: str
    fix_command: str
    description: str = ""
    error_family: str = ""
    error_type: str = ""
    package: str = ""
    package_version: str = ""
    os: str = ""
    architecture: str = ""
    python_version: str = ""
