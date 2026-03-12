"""Community hub client used by the nanobot web GUI."""

from __future__ import annotations

from typing import Any

import httpx


class GUICommunityService:
    """Minimal async client for the external Nanobot community hub."""

    def __init__(
        self,
        api_url: str | None = None,
        public_url: str | None = None,
        timeout_seconds: int = 8,
        api_token: str | None = None,
    ) -> None:
        api = str(api_url or "").strip().rstrip("/")
        public = str(public_url or "").strip().rstrip("/")
        if not api and public:
            api = f"{public}/api/v1"
        self.api_url = api
        self.public_url = public
        self.timeout_seconds = timeout_seconds
        self.api_token = str(api_token or "").strip()

    @property
    def enabled(self) -> bool:
        """Return whether the client is configured."""
        return bool(self.api_url)

    @property
    def can_write(self) -> bool:
        """Return whether authenticated write operations are configured."""
        return bool(self.enabled and self.api_token)

    async def overview(self) -> dict[str, Any]:
        """Return the marketplace overview payload."""
        return await self._get_json("/stats/overview")

    async def marketplace(
        self,
        *,
        query: str = "",
        category: str = "",
        language: str = "",
        runtime: str = "",
        min_reliability: int = 0,
        sort: str = "trending",
    ) -> dict[str, Any]:
        """Return the marketplace list."""
        return await self._get_json(
            "/marketplace",
            params={
                "q": query.strip(),
                "category": category.strip(),
                "language": language.strip(),
                "runtime": runtime.strip(),
                "min_reliability": max(0, int(min_reliability or 0)),
                "sort": sort.strip() or "trending",
            },
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

    async def showcase_detail(self, slug: str) -> dict[str, Any]:
        """Return one showcase entry."""
        return await self._get_json(f"/showcase/{slug}")

    async def vote_mcp(self, slug: str, *, vote_type: str, voter_key: str) -> dict[str, Any]:
        """Submit one community vote for an MCP."""
        return await self._post_json(f"/marketplace/{slug}/vote", json={"vote_type": vote_type, "voter_key": voter_key})

    async def vote_stack(self, slug: str, *, vote_type: str, voter_key: str) -> dict[str, Any]:
        """Submit one community vote for a stack."""
        return await self._post_json(f"/stacks/{slug}/vote", json={"vote_type": vote_type, "voter_key": voter_key})

    async def marketplace_fixes(
        self,
        slug: str,
        *,
        error_code: str = "",
        current_transport: str = "",
        current_timeout: int = 0,
        missing_runtimes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return bounded community fix suggestions for one MCP."""
        return await self._get_json(
            f"/marketplace/{slug}/fixes",
            params={
                "error_code": error_code.strip(),
                "current_transport": current_transport.strip(),
                "current_timeout": max(0, int(current_timeout or 0)),
                "missing_runtimes": ",".join(str(item).strip() for item in (missing_runtimes or []) if str(item).strip()),
            },
        )

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
        if not self.can_write:
            return {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.api_url}/submissions/mcp",
                json=payload,
                headers=self._write_headers(),
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    async def mark_install(self, slug: str) -> dict[str, Any]:
        """Record one community-driven MCP install."""
        return await self._post_json(f"/marketplace/{slug}/installs")

    async def mark_stack_import(self, slug: str) -> dict[str, Any]:
        """Record one community stack import."""
        return await self._post_json(f"/stacks/{slug}/imports")

    async def mark_showcase_import(self, slug: str) -> dict[str, Any]:
        """Record one community showcase import."""
        return await self._post_json(f"/showcase/{slug}/imports")

    async def submit_stack(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one stack to the community hub."""
        if not self.can_write:
            return {}
        return await self._post_json("/submissions/stack", json=payload, include_write_auth=True)

    async def submit_showcase(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit one showcase entry to the community hub."""
        if not self.can_write:
            return {}
        return await self._post_json("/submissions/showcase", json=payload, include_write_auth=True)

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch one JSON payload from the community hub."""
        if not self.enabled:
            return {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(f"{self.api_url}{path}", params=params)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    async def _post_json(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        include_write_auth: bool = False,
    ) -> dict[str, Any]:
        """Send one JSON POST request to the community hub."""
        if not self.enabled:
            return {}
        headers = self._write_headers() if include_write_auth else {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.api_url}{path}", json=json or {}, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    def _write_headers(self) -> dict[str, str]:
        """Return the authorization headers for admin-only hub writes."""
        if not self.api_token:
            return {}
        return {"Authorization": f"Bearer {self.api_token}"}
