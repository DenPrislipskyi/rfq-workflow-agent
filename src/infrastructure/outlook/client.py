from typing import Any

import httpx

from src.infrastructure.outlook.auth import GraphTokenProvider
from src.infrastructure.outlook.exceptions import GraphAPIError


class GraphClient:
    """Transport for Microsoft Graph: authentication, requests, error mapping.

    It knows nothing about mailboxes or subscriptions - that belongs to the
    modules built on top of it.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        token_provider: GraphTokenProvider,
        base_url: str,
    ) -> None:
        self._http_client = http_client
        self._token_provider = token_provider
        self._base_url = base_url.rstrip("/")

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, payload: dict) -> dict:
        return await self._request("POST", path, json=payload)

    async def patch(self, path: str, payload: dict) -> dict:
        return await self._request("PATCH", path, json=payload)

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        token = await self._token_provider.get_token()

        response = await self._http_client.request(
            method,
            f"{self._base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )

        if response.is_error:
            raise GraphAPIError(response.status_code, response.text)

        return response.json() if response.content else {}
