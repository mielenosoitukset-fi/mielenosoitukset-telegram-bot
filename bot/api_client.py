from __future__ import annotations

import logging

import httpx

from .config import settings
from . import token_manager

logger = logging.getLogger(__name__)


async def _get(path: str, params: dict | None = None) -> dict | list | None:
    headers = {}
    token = await token_manager.get_active_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{settings.api_base_url}{path}", params=params, headers=headers)
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
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{base}/api/v1/search_organizations", params={"q": query})
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
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{base}/api/v1/organizations", params=params)
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
