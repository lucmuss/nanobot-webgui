"""Community hub client used by the nanobot web GUI."""

from __future__ import annotations

from typing import Any

import httpx


class GUICommunityService:
    """Minimal async client for the external Nanobot community hub."""

    def __init__(self, api_url: str | None = None, public_url: str | None = None, timeout_seconds: int = 8) -> None:
        api = str(api_url or "").strip().rstrip("/")
        public = str(public_url or "").strip().rstrip("/")
        if not api and public:
            api = f"{public}/api/v1"
        self.api_url = api
        self.public_url = public
        self.timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        """Return whether the client is configured."""
        return bool(self.api_url)

    async def overview(self) -> dict[str, Any]:
        """Return the marketplace overview payload."""
        return await self._get_json("/stats/overview")

    async def marketplace(self, *, query: str = "", category: str = "", sort: str = "trending") -> dict[str, Any]:
        """Return the marketplace list."""
        return await self._get_json(
            "/marketplace",
            params={"q": query.strip(), "category": category.strip(), "sort": sort.strip() or "trending"},
        )

    async def marketplace_detail(self, slug: str) -> dict[str, Any]:
        """Return one MCP marketplace entry."""
        return await self._get_json(f"/marketplace/{slug}")

    async def resolve_repository(self, repo_url: str) -> dict[str, Any] | None:
        """Try to match one repository URL to a marketplace entry."""
        payload = await self._get_json("/marketplace/resolve", params={"repo_url": repo_url.strip()})
        match = payload.get("match")
        return match if isinstance(match, dict) else None

    async def stacks(self, *, query: str = "") -> dict[str, Any]:
        """Return stack presets."""
        return await self._get_json("/stacks", params={"q": query.strip()})

    async def stack_detail(self, slug: str) -> dict[str, Any]:
        """Return one stack detail."""
        return await self._get_json(f"/stacks/{slug}")

    async def showcase(self, *, query: str = "", category: str = "") -> dict[str, Any]:
        """Return showcase entries."""
        return await self._get_json("/showcase", params={"q": query.strip(), "category": category.strip()})

    async def ingest_telemetry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one anonymous telemetry event to the hub."""
        if not self.enabled:
            return {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.api_url}/telemetry/events", json=payload)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    async def submit_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one MCP repository to the community hub."""
        if not self.enabled:
            return {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.api_url}/submissions/mcp", json=payload)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch one JSON payload from the community hub."""
        if not self.enabled:
            return {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(f"{self.api_url}{path}", params=params)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
