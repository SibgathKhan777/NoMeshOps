"""Local knowledge base: a JSON file with the same shape as the DynamoDB table."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from app.config import settings

_lock = threading.Lock()


def _path() -> str:
    os.makedirs(settings.local_data_dir, exist_ok=True)
    return os.path.join(settings.local_data_dir, "fixes.json")


def _load() -> dict[str, dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(items: dict[str, dict]) -> None:
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, sort_keys=True)
    os.replace(tmp, _path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_fix(signature: str) -> dict | None:
    return _load().get(signature)


def get_family_fixes(family: str) -> list[dict]:
    items = [i for i in _load().values() if i.get("error_family") == family]
    items.sort(key=lambda i: int(i.get("success_count", 0)) - int(i.get("failure_count", 0)), reverse=True)
    return items


def put_fix(error: dict, fingerprint: dict, fix_command: str, description: str, source: str) -> dict:
    item = {
        "error_signature": error["signature"], "error_family": error.get("family", ""),
        "error_type": error.get("error_type", ""), "package": error.get("package", ""),
        "package_version": error.get("package_version", ""), "normalized_error": error.get("normalized", ""),
        "os": fingerprint.get("os_key", ""), "os_pretty": fingerprint.get("os_pretty", ""),
        "architecture": fingerprint.get("arch", ""), "python_version": fingerprint.get("python_minor", ""),
        "fix_command": fix_command, "description": description, "source": source,
        "success_count": 1, "failure_count": 0, "first_seen_at": _now(), "last_verified_at": _now(),
    }
    with _lock:
        items = _load(); items[error["signature"]] = item; _save(items)
    return item


def record_outcome(signature: str, success: bool) -> None:
    with _lock:
        items = _load()
        it = items.get(signature)
        if not it:
            return
        it["success_count" if success else "failure_count"] = int(it.get("success_count" if success else "failure_count", 0)) + 1
        if success:
            it["last_verified_at"] = _now()
        _save(items)


def list_fixes(limit: int = 50) -> list[dict]:
    return list(_load().values())[:limit]


def delete_fix(signature: str) -> None:
    with _lock:
        items = _load(); items.pop(signature, None); _save(items)
