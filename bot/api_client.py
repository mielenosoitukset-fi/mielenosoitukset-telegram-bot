from __future__ import annotations

import asyncio
import logging

import httpx

from .config import settings
from . import token_manager

logger = logging.getLogger(__name__)


# Server limit is 10 req/s per IP, and other bots share our IP. Stay well below.
MIN_INTERVAL = 0.25  # seconds between requests -> max 4/s
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0

_client: httpx.AsyncClient | None = None
_pace_lock = asyncio.Lock()
_next_slot = 0.0


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _pace() -> None:
    """Global throttle: every request waits for its own time slot."""
    global _next_slot
    loop = asyncio.get_running_loop()
    async with _pace_lock:
        now = loop.time()
        wait = max(0.0, _next_slot - now)
        _next_slot = max(now, _next_slot) + MIN_INTERVAL
    if wait:
        await asyncio.sleep(wait)


def _retry_after(r: httpx.Response, attempt: int) -> float:
    raw = r.headers.get("Retry-After")
    if raw:
        try:
            return min(max(float(raw), 0.0), MAX_RETRY_WAIT)
        except ValueError:
            pass
    return min(2.0 ** attempt, MAX_RETRY_WAIT)


async def _request(url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
    """GET with pacing, shared client, and backoff on 429/5xx/transport errors."""
    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        await _pace()
        try:
            r = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                raise
            delay = min(2.0 ** attempt, MAX_RETRY_WAIT)
            logger.warning("API transport error %s on %s; retry in %.1fs", exc.__class__.__name__, url, delay)
            await asyncio.sleep(delay)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == MAX_RETRIES:
                return r
            delay = _retry_after(r, attempt)
            logger.warning("API %s on %s; retry in %.1fs", r.status_code, url, delay)
            await asyncio.sleep(delay)
            continue
        if r.status_code in (401, 403):
            logger.error("API %s on %s: token missing, expired or not allowed. Needs a new API token.", r.status_code, url)
        return r
    raise last_exc or RuntimeError("unreachable")


async def _get(path: str, params: dict | None = None) -> dict | list | None:
    headers = {}
    token = await token_manager.get_active_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await _request(f"{settings.api_base_url}{path}", params=params, headers=headers)
    r.raise_for_status()
    return r.json()


async def fetch_upcoming_demos(
    city: str | None = None,
    organization_id: str | None = None,
    parent_id: str | None = None,
    max_days_till: int = 14,
    per_page: int = 50,
    page: int = 1,
) -> dict:
    params: dict = {
        "per_page": per_page,
        "page": page,
        "in_past": "false",
        "max_days_till": max_days_till,
        "include_cancelled": "false",
    }
    if city:
        params["city"] = city
    if organization_id:
        params["organization_id"] = organization_id
    if parent_id:
        params["parent_id"] = parent_id

    data = await _get("/demonstrations", params)
    if not data:
        return {"results": [], "total": 0, "total_pages": 0, "page": 1}
    return data


async def fetch_all_upcoming_demos(
    max_days_till: int = 90,
    per_page: int = 100,
) -> list[dict]:
    all_demos: list[dict] = []
    page = 1
    while True:
        data = await fetch_upcoming_demos(max_days_till=max_days_till, per_page=per_page, page=page)
        results = data.get("results", [])
        all_demos.extend(results)
        total_pages = data.get("total_pages", 1)
        if page >= total_pages or not results:
            break
        page += 1
    return all_demos


async def fetch_demo_detail(demo_id: str) -> dict:
    data = await _get(f"/demonstrations/{demo_id}")
    return data or {}


async def fetch_all_demos_for_orgs(org_ids: list[str], max_days_till: int = 14) -> list[dict]:
    all_demos: list[dict] = []
    for org_id in org_ids:
        demos_data = await fetch_upcoming_demos(organization_id=org_id, max_days_till=max_days_till)
        all_demos.extend(demos_data.get("results", []))
    return all_demos


async def fetch_all_demos_for_cities(cities: list[str], max_days_till: int = 14) -> list[dict]:
    if not cities:
        return []
    data = await fetch_upcoming_demos(city=",".join(cities), max_days_till=max_days_till)
    return data.get("results", [])


async def fetch_all_demos_for_chains(parent_ids: list[str], max_days_till: int = 14) -> list[dict]:
    all_demos: list[dict] = []
    for pid in parent_ids:
        demos_data = await fetch_upcoming_demos(parent_id=pid, max_days_till=max_days_till)
        all_demos.extend(demos_data.get("results", []))
    return all_demos


async def search_organizations(query: str) -> list[dict]:
    """Search organizations by name via the public search endpoint."""
    if len(query) < 2:
        return []
    base = settings.api_base_url.replace("/api", "")
    r = await _request(f"{base}/api/v1/search_organizations", params={"q": query})
    if r.status_code != 200:
        return []
    data = r.json()
    return data if isinstance(data, list) else []


async def fetch_organizations(page: int = 1, per_page: int = 100, search: str = "") -> dict:
    """List organizations via the public /api/v1/organizations helper.

    Returns an empty dict if the endpoint is not (yet) available.
    """
    base = settings.api_base_url.replace("/api", "")
    params: dict = {"page": page, "per_page": per_page}
    if search:
        params["search"] = search
    r = await _request(f"{base}/api/v1/organizations", params=params)
    if r.status_code != 200:
        return {}
    return r.json()


async def fetch_all_organizations(per_page: int = 100) -> list[dict]:
    """Fetch the full organization catalog; empty list if unavailable."""
    orgs: list[dict] = []
    page = 1
    while True:
        data = await fetch_organizations(page=page, per_page=per_page)
        results = data.get("organizations", [])
        orgs.extend(results)
        total_pages = data.get("total_pages", 1)
        if page >= total_pages or not results:
            break
        page += 1
    return orgs
