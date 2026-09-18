"""Runtime settings, read from environment (.env is loaded if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    aws_region: str = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
    fixes_table: str = os.getenv("FIXES_TABLE", "deployment_fixes")
    logs_bucket: str = os.getenv("LOGS_BUCKET", "")
    bedrock_model_id: str = os.getenv(
        "BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
    )
    target_workdir: str = os.getenv("TARGET_WORKDIR", "/opt/nomeshops")
    ssm_timeout_seconds: int = int(os.getenv("SSM_TIMEOUT_SECONDS", "900"))
    max_fix_attempts: int = int(os.getenv("MAX_FIX_ATTEMPTS", "3"))
    max_llm_attempts: int = int(os.getenv("MAX_LLM_ATTEMPTS", "1"))
    preempt_predicted_fixes: bool = _bool(os.getenv("PREEMPT_PREDICTED_FIXES"), True)


settings = Settings()
