from __future__ import annotations

import httpx

from .config import settings
from . import token_manager


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
) -> list[dict]:
    params: dict = {
        "per_page": per_page,
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
        return []
    return data.get("results", [])


async def fetch_demo_detail(demo_id: str) -> dict:
    data = await _get(f"/demonstrations/{demo_id}")
    return data or {}


async def fetch_all_demos_for_orgs(org_ids: list[str], max_days_till: int = 14) -> list[dict]:
    all_demos = []
    for org_id in org_ids:
        demos = await fetch_upcoming_demos(organization_id=org_id, max_days_till=max_days_till)
        all_demos.extend(demos)
    return all_demos


async def fetch_all_demos_for_cities(cities: list[str], max_days_till: int = 14) -> list[dict]:
    if not cities:
        return []
    params: dict = {
        "per_page": 50,
        "in_past": "false",
        "max_days_till": max_days_till,
        "city": ",".join(cities),
    }
    data = await _get("/demonstrations", params)
    if not data:
        return []
    return data.get("results", [])


async def fetch_all_demos_for_chains(parent_ids: list[str], max_days_till: int = 14) -> list[dict]:
    all_demos = []
    for pid in parent_ids:
        demos = await fetch_upcoming_demos(parent_id=pid, max_days_till=max_days_till)
        all_demos.extend(demos)
    return all_demos


async def search_organizations(query: str) -> list[dict]:
    """Search organizations by name via the (undocumented) search endpoint."""
    if len(query) < 2:
        return []
    base = settings.api_base_url.replace("/api", "")
    token = await token_manager.get_active_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{base}/api/v1/search_organizations", params={"q": query}, headers=headers)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
