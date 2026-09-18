"""Step 8: write the full attempt record to S3 as the audit trail."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.aws.session import client
from app.config import settings


def store_attempt(record: dict) -> str | None:
    if not settings.logs_bucket:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"attempts/{ts}-{record.get('instance_id', 'unknown')}-{record.get('run_id', '')}.json"
    client("s3").put_object(
        Bucket=settings.logs_bucket,
        Key=key,
        Body=json.dumps(record, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    return key


def list_attempts(limit: int = 20) -> list[dict]:
    if not settings.logs_bucket:
        return []
    resp = client("s3").list_objects_v2(Bucket=settings.logs_bucket, Prefix="attempts/", MaxKeys=1000)
    items = sorted(resp.get("Contents", []), key=lambda o: o["LastModified"], reverse=True)[:limit]
    return [{"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].isoformat()} for o in items]
