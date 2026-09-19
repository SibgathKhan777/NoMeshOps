"""Access to the hosted tool.

Two kinds of session, mirroring how `aws login` works:

* An OPERATOR signs in on the web with the access key configured on the server. That sets a cookie.
* A CLI signs in with the DEVICE FLOW: it asks the server for a short code, the person opens the
  verification page, an operator approves the code, and the CLI polls until it receives a bearer token.

Sessions are kept in memory and mirrored to `<LOCAL_DATA_DIR>/sessions.json` so a restart does not sign
everyone out. Tokens are random, never derived from anything, and expire on their own.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, asdict, field

from fastapi import Depends, HTTPException, Request

from app.config import settings

SESSION_TTL_S = 12 * 3600
DEVICE_TTL_S = 10 * 60
DEVICE_POLL_S = 3
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"   # no vowels or look-alikes: nothing rude, nothing ambiguous
COOKIE = "nomeshops_web"

_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _data_dir() -> str:
    d = settings.local_data_dir or