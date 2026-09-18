"""Step 6 (miss path): ask Claude on Amazon Bedrock for ONE structured fix. Only called when
the rules table and the knowledge base both miss."""
from __future__ import annotations

import json
import re

from app.aws.session import client
from app.config import settings

SYSTEM_PROMPT = """You are a deployment repair engine. A Python project failed to deploy on a Linux machine.
You will be given the machine fingerprint, the project's dependency manifest, and the failure output.

Respond with ONE JSON object and nothing else:
{"fix_command": "<shell command(s)>", "stage": "post", "description": "<one line>", "confidence": <0.0-1.0>, "rationale": "<two sentences max>"}

"stage" is "post" (default: runs after the venv exists, right before pip install) or "pre" (runs before
`git clone`; use only for system bootstrap such as installing git or python itself).

Execution contract for fix_command (do not violate it):
- Runs as root, in the freshly cloned project directory, under bash.
- The project's virtualenv (.venv) is already created and FIRST on PATH: `pip` and `python` are the venv's.
- It runs immediately BEFORE `pip install -r requirements.txt` (or `pip install .`). Anything you install
  with pip persists into that step; edits to requirements.txt/pyproject.toml take effect.
- The system package manager is available (see fingerprint: apt-get / dnf / yum). Use non-interactive flags.
- No interactive prompts, no reboots, no editing of files outside the project dir except package installs.
- Prefer the smallest fix that makes THIS project install and run: install a missing system library, pin or
  bump a package to a version with wheels for this Python, or fix a typo in the manifest with sed.
- Never use destructive commands (rm -rf on system paths, mkfs, dd, shutdown). Never curl|sh.
"""

# A deny-list is a backstop, not a sandbox: the real containment is that fixes run on a disposable target.
# `(?:-{1,2}[\w-]+\s+)*` tolerates interleaved flags, so `rm -rf --no-preserve-root /` cannot slip past a
# rule written to catch `rm -rf /`.
_RM = r"rm\s+(?:-{1,2}[\w-]+\s+)*"
DENY = re.compile(
    rf"{_RM}/(?!\S)|"
    rf"{_RM}/(bin|boot|dev|etc|home|lib|proc|root|sbin|sys|usr|var)\b|"
    rf"{_RM}(\$HOME|~)(/\s*)?(?!\S)|"
    r"mkfs|\bdd\s+if=|shutdown|reboot|halt\b|:\(\)\s*\{|>\s*/dev/(sd|nvme|xvd)|"
    r"chmod\s+(-{1,2}[\w-]+\s+)*[0-7]*777\s+/(?!\S)|chown\s+(-{1,2}[\w-]+\s+)*\S+\s+/(?!\S)|"
    r"crontab\s+-r|\buserdel\b|\bhistory\s+-c\b|"
    r"curl[^|]*\|\s*(ba)?sh|wget[^|]*\|\s*(ba)?sh",
    re.IGNORECASE,
)


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def propose_fix(fingerprint: dict, scan: dict, error: dict, failure_output: str,
                prior_attempts: list[dict], hint: dict | None = None) -> dict:
    deps = "\n".join(d.get("raw") or d.get("name", "") for d in scan.get("dependencies", [])) or "(none found)"
    prior = ""
    if prior_attempts:
        lines = []
        for a in prior_attempts:
            fix = a.get("fix") or {}
            if fix:
                lines.append(f"- source={fix.get('source')} command={fix.get('fix_command')!r} -> {a.get('result')}")
        if lines:
            prior = "Fixes already tried this run (do not repeat them):\n" + "\n".join(lines)
    hint_txt = ""
    if hint:
        hint_txt = (f"A verified fix for the same error family on a different platform "
                    f"({hint.get('os')}/{hint.get('python_version')}) was: {hint.get('fix_command')!r}. "
                    f"Adapt it to this machine if it applies.")
    user = f"""MACHINE FINGERPRINT
{json.dumps({k: v for k, v in fingerprint.items() if k != 'raw'}, indent=2)}

PROJECT MANIFEST ({'requirements.txt' if scan.get('has_requirements') else 'pyproject.toml' if scan.get('has_pyproject') else 'unknown'})
requires-python: {scan.get('python_requires') or '(unspecified)'}
{deps}

FAILURE (stage={error.get('stage')}, type={error.get('error_type')}, package={error.get('package') or '?'} {error.get('package_version') or ''})
{failure_output[-6000:]}

{prior}
{hint_txt}
Return the JSON object now."""

    if settings.llm_backend == "none":
        raise RuntimeError("LLM_BACKEND=none: LLM fallback is disabled for this run")
    if settings.llm_backend == "anthropic":
        from app.local.llm_anthropic import converse
        text, usage = converse(SYSTEM_PROMPT, user)
        model_id = settings.anthropic_model
    else:
        br = client("bedrock-runtime")
        resp = br.converse(
            modelId=settings.bedrock_model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": 800, "temperature": 0.2},
        )
        text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
        usage = resp.get("usage", {})
        model_id = settings.bedrock_model_id
    data = _extract_json(text)
    cmd = str(data.get("fix_command", "")).strip()
    if not cmd:
        raise ValueError("model returned an empty fix_command")
    if DENY.search(cmd):
        raise ValueError(f"model proposed a command blocked by the safety filter: {cmd!r}")
    stage = "pre" if str(data.get("stage", "post")).lower() == "pre" else "post"
    return {
        "source": "bedrock" if settings.llm_backend == "bedrock" else f"llm-{settings.llm_backend}",
        "fix_command": cmd,
        "stage": stage,
        "description": str(data.get("description", ""))[:300],
        "confidence": float(data.get("confidence", 0) or 0),
        "rationale": str(data.get("rationale", ""))[:600],
        "model_id": model_id,
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
    }
