"""Optional LLM backend: the Anthropic API directly (for laptops without Bedrock access)."""
from __future__ import annotations

import os

from app.config import settings


def converse(system: str, user: str, max_tokens: int = 800, temperature: float = 0.2) -> tuple[str, dict]:
    try:
        import anthropic  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pip install anthropic to use LLM_BACKEND=anthropic") from e
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic()
    msg = client.messages.create(model=settings.anthropic_model, max_tokens=max_tokens, temperature=temperature,
                                 system=system, messages=[{"role": "user", "content": user}])
    text = "".join(getattr(b, "text", "") for b in msg.content)
    usage = {"inputTokens": msg.usage.input_tokens, "outputTokens": msg.usage.output_tokens}
    return text, usage
