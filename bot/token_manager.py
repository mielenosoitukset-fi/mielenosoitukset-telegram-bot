from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import settings

_TOKEN_FILE = Path("data/token.json")


class TokenError(Exception):
    pass


def _read_stored() -> dict | None:
    if not _TOKEN_FILE.exists():
        return None
    try:
        return json.loads(_TOKEN_FILE.read_text())
    except Exception:
        return None


def _write_stored(payload: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(payload, indent=2))


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def exchange_for_long_lived(short_token: str) -> dict:
    """Exchange a short-lived token for a long-lived (90d) token via the API."""
    headers = {"Authorization": f"Bearer {short_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{settings.api_base_url}/token/long_lived", headers=headers)
        if r.status_code != 201:
            body = r.text
            raise TokenError(f"Token exchange failed ({r.status_code}): {body}")
        payload = r.json()

    long_token = payload.get("token")
    if not long_token:
        raise TokenError("Long-lived token missing from response")
    return {
        "token": long_token,
        "expires_at": payload.get("expires_at"),
        "message": payload.get("message"),
    }


async def get_active_token() -> str:
    """Return a valid token, renewing or re-exchanging if necessary."""
    stored = _read_stored()

    # Take the env-provided token as highest priority (for quick setup).
    env_token = settings.api_token.strip()
    if env_token:
        # If we have an exchanged long token already, prefer it if still valid.
        if stored and stored.get("token") and not _is_expired(stored.get("expires_at")):
            return stored["token"]
        return env_token

    if not stored or not stored.get("token"):
        raise TokenError("No API token configured. Set it via /config or API_TOKEN env var.")

    if not _is_expired(stored.get("expires_at")):
        return stored["token"]

    # Token expired: if we kept the original short token, re-exchange.
    source_token = stored.get("source_token")
    if not source_token:
        raise TokenError("Token expired and no source token to re-exchange from.")

    try:
        result = await exchange_for_long_lived(source_token)
    except Exception as e:
        raise TokenError(f"Could not refresh stored token: {e}")

    result["source_token"] = source_token
    _write_stored(result)
    return result["token"]


def _is_expired(iso_value) -> bool:
    dt = _parse_iso(iso_value)
    if dt is None:
        return False  # treat unparseable as not-expired, rely on server errors
    return dt <= datetime.now(timezone.utc)


def store_token(payload: dict) -> None:
    _write_stored(payload)


def is_configured() -> bool:
    if settings.api_token.strip():
        return True
    stored = _read_stored()
    return bool(stored and stored.get("token"))


def is_configured_from_env() -> bool:
    return bool(settings.api_token.strip())
