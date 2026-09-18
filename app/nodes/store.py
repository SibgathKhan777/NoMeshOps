"""Step 8: write the full attempt record to S3 as the audit trail."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.aws.session import client
from app.config import settings


def store_attempt(record: dict) -> str | None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"attempts/{ts}-{record.get('instance_id', 'unknown')}-{record.get('run_id', '')}.json"
    if settings.store_backend == "local":
        import os
        path = os.path.join(settings.local_data_dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, default=str)
        return path
    if not settings.logs_bucket:
        return None
    client("s3").put_object(
        Bucket=settings.logs_bucket,
        Key=key,
        Body=json.dumps(record, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    return key


def list_attempts(limit: int = 20) -> list[dict]:
    if settings.store_backend == "local":
        import os
        d = os.path.join(settings.local_data_dir, "attempts")
        if not os.path.isdir(d):
            return []
        names = sorted(os.listdir(d), reverse=True)[:limit]
        return [{"key": os.path.join(d, n), "size": os.path.getsize(os.path.join(d, n)),
                 "last_modified": datetime.fromtimestamp(os.path.getmtime(os.path.join(d, n)), timezone.utc).isoformat()} for n in names]
    if not settings.logs_bucket:
        return []
    resp = client("s3").list_objects_v2(Bucket=settings.logs_bucket, Prefix="attempts/", MaxKeys=1000)
    items = sorted(resp.get("Contents", []), key=lambda o: o["LastModified"], reverse=True)[:limit]
    return [{"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"].isoformat()} for o in items]
