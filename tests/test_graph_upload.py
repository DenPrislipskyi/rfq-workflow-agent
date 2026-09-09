"""Sending a big attachment to Graph's pre-authorized upload URL.

Two rules Graph enforces and nothing else would catch: the chunk size, and the
absence of our own bearer token. Both fail once, in production, with an error
that does not say which.
"""

import httpx
import pytest

from src.infrastructure.outlook.client import UPLOAD_CHUNK_BYTES, GraphClient
from src.infrastructure.outlook.exceptions import GraphAPIError

URL = "https://upload.example.invalid/session?token=abc"


class Token:
    async def get_token(self) -> str:
        return "our-own-token"


def client(handler) -> GraphClient:
    transport = httpx.MockTransport(handler)
    return GraphClient(
        httpx.AsyncClient(transport=transport), Token(), "https://graph.example.invalid"
    )


def recorder(status: int = 200):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return handle, seen


def test_the_chunk_size_obeys_both_of_graphs_rules() -> None:
    """At most 4 MB, and a multiple of 320 KiB. Graph rejects the whole upload
    over either, and the message it sends back names neither."""
    assert UPLOAD_CHUNK_BYTES % (320 * 1024) == 0
    assert UPLOAD_CHUNK_BYTES <= 4 * 1024 * 1024


async def test_a_small_body_goes_in_one_piece() -> None:
    handle, seen = recorder()

    await client(handle).upload(URL, b"x" * 100)

    assert len(seen) == 1
    assert seen[0].headers["Content-Range"] == "bytes 0-99/100"


async def test_a_body_larger_than_one_chunk_is_split_and_the_ranges_join_up() -> None:
    handle, seen = recorder()
    size = UPLOAD_CHUNK_BYTES * 2 + 17

    await client(handle).upload(URL, b"x" * size)

    assert [request.headers["Content-Range"] for request in seen] == [
        f"bytes 0-{UPLOAD_CHUNK_BYTES - 1}/{size}",
        f"bytes {UPLOAD_CHUNK_BYTES}-{UPLOAD_CHUNK_BYTES * 2 - 1}/{size}",
        f"bytes {UPLOAD_CHUNK_BYTES * 2}-{size - 1}/{size}",
    ]
    assert sum(len(request.content) for request in seen) == size


async def test_our_token_is_not_sent_to_the_upload_url() -> None:
    """The URL carries its own credentials in the query string, and Graph
    refuses the request when a bearer token turns up as well."""
    handle, seen = recorder()

    await client(handle).upload(URL, b"x" * 10)

    assert "Authorization" not in seen[0].headers


async def test_a_chunk_that_is_refused_stops_the_upload() -> None:
    handle, seen = recorder(status=413)

    with pytest.raises(GraphAPIError):
        await client(handle).upload(URL, b"x" * (UPLOAD_CHUNK_BYTES * 2))

    assert len(seen) == 1, "the second chunk was not sent after the first was refused"
