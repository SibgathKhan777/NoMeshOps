"""Step 6/8: DynamoDB knowledge base of verified fixes. Exact-signature match, with a
context-free `family` index as a second chance (verification remains the gate)."""
from __future__ import annotations

from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key

from app.aws.session import session
from app.config import settings

FAMILY_INDEX = "family-index"


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
    resp = _table().get_item(Key={"error_signature": signature})
    item = resp.get("Item")
    return _plain(item) if item else None


def get_family_fixes(family: str) -> list[dict]:
    resp = _table().query(IndexName=FAMILY_INDEX, KeyConditionExpression=Key("error_family").eq(family))
    items = [_plain(i) for i in resp.get("Items", [])]
    items.sort(key=lambda i: (int(i.get("success_count", 0)) - int(i.get("failure_count", 0))), reverse=True)
    return items


def put_fix(error: dict, fingerprint: dict, fix_command: str, description: str, source: str) -> dict:
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
    _table().put_item(Item=item)
    return item


def record_outcome(signature: str, success: bool) -> None:
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
    resp = _table().scan(Limit=limit)
    return [_plain(i) for i in resp.get("Items", [])]


def delete_fix(signature: str) -> None:
    _table().delete_item(Key={"error_signature": signature})
