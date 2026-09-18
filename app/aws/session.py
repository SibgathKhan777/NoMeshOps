"""Shared boto3 session. Honors AWS_PROFILE locally; inside ECS the task role is used."""
from __future__ import annotations

import boto3

from app.config import settings

_session: boto3.Session | None = None


def session() -> boto3.Session:
    global _session
    if _session is None:
        _session = boto3.Session(region_name=settings.aws_region)
    return _session


def client(name: str):
    return session().client(name)
