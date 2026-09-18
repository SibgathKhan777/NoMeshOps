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
    max_transient_retries: int = int(os.getenv("MAX_TRANSIENT_RETRIES", "1"))
    preempt_predicted_fixes: bool = _bool(os.getenv("PREEMPT_PREDICTED_FIXES"), True)
    # Backends. AWS is the default; "local" mode needs no AWS account at all.
    executor: str = os.getenv("EXECUTOR", "ssm")            # ssm | docker
    kb_backend: str = os.getenv("KB_BACKEND", "dynamodb")    # dynamodb | local
    store_backend: str = os.getenv("STORE_BACKEND", "s3")    # s3 | local
    llm_backend: str = os.getenv("LLM_BACKEND", "bedrock")   # bedrock | anthropic | none
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    local_data_dir: str = os.getenv("LOCAL_DATA_DIR", ".nomeshops")

    @property
    def llm_label(self) -> str:
        return {"bedrock": f"Bedrock ({self.bedrock_model_id})", "anthropic": f"Anthropic API ({self.anthropic_model})",
                "none": "no LLM (disabled)"}.get(self.llm_backend, self.llm_backend)


settings = Settings()
