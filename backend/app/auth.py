"""Minimal signed-token auth for reviewer accounts (configured via REVIEWER_ACCOUNTS).

It protects the hosted demo and its LLM budget and attributes every decision in the
audit history to a named reviewer. It is not a substitute for SSO in production.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Header, HTTPException

from .config import settings

TOKEN_TTL_S = 12 * 3600


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(settings.auth_secret.encode(), payload, hashlib.sha256).digest()).decode()


def issue_token(username: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": username, "exp": int(time.time()) + TOKEN_TTL_S}).encode())
    return payload.decode() + "." + _sign(payload)


def verify_token(token: str) -> str | None:
    try:
        payload, sig = token.split(".", 1)
        if not hmac.compare_digest(_sign(payload.encode()), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if data["exp"] < time.time() or data["u"] not in settings.reviewer_accounts:
            return None
        return data["u"]
    except Exception:
        return None


def check_credentials(username: str, password: str) -> bool:
    expected = settings.reviewer_accounts.get((username or "").strip())
    return expected is not None and hmac.compare_digest(expected.encode(), (password or "").encode())


def current_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Sign in to continue"})
    user = verify_token(authorization.split(" ", 1)[1].strip())
    if not user:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Session expired; sign in again"})
    return user
