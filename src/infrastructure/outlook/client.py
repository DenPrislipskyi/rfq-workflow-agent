from typing import Any

import httpx

from src.infrastructure.outlook.auth import GraphTokenProvider
from src.infrastructure.outlook.exceptions import GraphAPIError

# Graph accepts at most 4 MB per chunk and requires every chunk but the last to
# be a multiple of 320 KiB. Ten of those is the largest size that satisfies both.
UPLOAD_CHUNK_BYTES = 10 * 320 * 1024


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

    async def post(self, path: str, payload: dict | None = None) -> dict:
        """`payload=None` sends no body at all.

        Not the same as `{}`: Graph's `/send` takes no body, and an empty JSON
        object is enough for it to answer 400.
        """
        if payload is None:
            return await self._request("POST", path)
        return await self._request("POST", path, json=payload)

    async def patch(self, path: str, payload: dict) -> dict:
        return await self._request("PATCH", path, json=payload)

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path)

    async def get_bytes(self, path: str) -> bytes:
        """Fetch a raw body rather than JSON - Graph's `/$value` endpoints.

        Separate from `get` because an attachment's bytes are not a dict and
        must never be pushed through `.json()`.
        """
        token = await self._token_provider.get_token()

        response = await self._http_client.get(
            f"{self._base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.is_error:
            raise GraphAPIError(response.status_code, response.text)

        return response.content

    async def upload(self, url: str, data: bytes, *, timeout_s: float | None = None) -> None:
        """Send a large body to a pre-authorized upload URL, in chunks.

        Deliberately not through `_request`: the URL Graph hands back carries
        its own credentials in the query string, and adding our bearer token
        makes it refuse the request.

        Every chunk but the last has to be a multiple of 320 KiB - Graph's own
        rule, not a preference - and the server answers the last one with 201
        rather than 200.
        """
        total = len(data)
        for start in range(0, total, UPLOAD_CHUNK_BYTES):
            chunk = data[start : start + UPLOAD_CHUNK_BYTES]
            end = start + len(chunk) - 1

            response = await self._http_client.put(
                url,
                content=chunk,
                headers={"Content-Range": f"bytes {start}-{end}/{total}"},
                timeout=timeout_s or self._http_client.timeout,
            )

            if response.is_error:
                raise GraphAPIError(response.status_code, response.text)

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
