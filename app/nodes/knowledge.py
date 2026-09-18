"""Step 6/8: DynamoDB knowledge base of verified fixes. Exact-signature match, with a
context-free `family` index as a second chance (verification remains the gate)."""
from __future__ import annotations

from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key

from app.aws.session import session
from app.config import settings

FAMILY_INDEX = "family-index"


def _local():
    from app.local import kb_local
    return kb_local


def _is_local() -> bool:
    return settings.kb_backend == "local"


def _table():
    return session().resource("dynamodb").Table(settings.fixes_table)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        out[k] = int(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (int, float)) else v
    return out


def get_fix(signature: str) -> dict | None:
    if _is_local():
        return _local().get_fix(signature)
    resp = _table().get_item(Key={"error_signature": signature})
    item = resp.get("Item")
    return _plain(item) if item else None


def get_family_fixes(family: str) -> list[dict]:
    if _is_local():
        return _local().get_family_fixes(family)
    resp = _table().query(IndexName=FAMILY_INDEX, KeyConditionExpression=Key("error_family").eq(family))
    items = [_plain(i) for i in resp.get("Items", [])]
    items.sort(key=lambda i: (int(i.get("success_count", 0)) - int(i.get("failure_count", 0))), reverse=True)
    return items


def put_fix(error: dict, fingerprint: dict, fix_command: str, description: str, source: str) -> dict:
    if _is_local():
        return _local().put_fix(error, fingerprint, fix_command, description, source)
    item = {
        "error_signature": error["signature"],
        "error_family": error.get("family", ""),
        "error_type": error.get("error_type", ""),
        "package": error.get("package", ""),
        "package_version": error.get("package_version", ""),
        "normalized_error": error.get("normalized", ""),
        "os": fingerprint.get("os_key", ""),
        "os_pretty": fingerprint.get("os_pretty", ""),
        "architecture": fingerprint.get("arch", ""),
        "python_version": fingerprint.get("python_minor", ""),
        "fix_command": fix_command,
        "description": description,
        "source": source,
        "success_count": 1,
        "failure_count": 0,
        "first_seen_at": _now(),
        "last_verified_at": _now(),
    }
    existing = None
    try:
        existing = get_fix(error["signature"])
    except Exception:  # noqa: BLE001
        existing = None
    if existing:
        # Never discard history on re-learn: keep first_seen_at and the failure tally, and remember what
        # this fix replaced. A plain overwrite would silently reset the counters the KB is judged on.
        item["first_seen_at"] = existing.get("first_seen_at", item["first_seen_at"])
        item["failure_count"] = int(existing.get("failure_count", 0))
        if existing.get("fix_command") == fix_command:
            item["success_count"] = int(existing.get("success_count", 0)) + 1
        else:
            item["superseded_fix"] = existing.get("fix_command", "")
            item["superseded_at"] = _now()
    _table().put_item(Item=item)
    return item


def record_outcome(signature: str, success: bool) -> None:
    if _is_local():
        return _local().record_outcome(signature, success)
    field = "success_count" if success else "failure_count"
    expr = f"ADD {field} :one SET last_verified_at = :t" if success else f"ADD {field} :one"
    values = {":one": 1}
    if success:
        values[":t"] = _now()
    _table().update_item(
        Key={"error_signature": signature},
        UpdateExpression=expr,
        ExpressionAttributeValues=values,
    )


def list_fixes(limit: int = 50) -> list[dict]:
    if _is_local():
        return _local().list_fixes(limit)
    resp = _table().scan(Limit=limit)
    return [_plain(i) for i in resp.get("Items", [])]


def delete_fix(signature: str) -> None:
    if _is_local():
        return _local().delete_fix(signature)
    _table().delete_item(Key={"error_signature": signature})
