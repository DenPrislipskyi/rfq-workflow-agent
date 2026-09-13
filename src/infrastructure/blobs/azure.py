"""Azure Blob Storage, through the async SDK.

One container, flat keys with slashes in them. Blob storage has no folders -
`2026-09-13T17-03-03Z__b7d5d04d/attachments/Requisition.xlsx` is one name with
slashes - but the portal draws them as folders, which is worth having when
somebody is looking for a file by eye.

The container is private and stays private. Nothing here hands out a URL: the
browser asks the API for a file and the API fetches it, so a link to an
attachment is never a link anybody else can follow.
"""

import logging

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import ContentSettings

logger = logging.getLogger(__name__)

# Azure allows 1024 characters in a blob name. `_safe_name` already trims each
# filename to 120, so this only matters for a record id nobody expected.
MAX_KEY = 1024


class AzureBlobs:
    """The container, as somewhere to put bytes."""

    def __init__(self, connection_string: str, container: str) -> None:
        self._client = BlobServiceClient.from_connection_string(connection_string)
        self._container = container

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        settings = ContentSettings(content_type=content_type) if content_type else None
        blob = self._client.get_blob_client(self._container, _key(key))
        await blob.upload_blob(data, overwrite=True, content_settings=settings)

    async def get(self, key: str) -> bytes | None:
        blob = self._client.get_blob_client(self._container, _key(key))
        try:
            stream = await blob.download_blob()
        except ResourceNotFoundError:
            # An attachment nobody kept, or a key from an older record. The
            # caller turns this into a 404; it is not worth a stack trace.
            return None
        return await stream.readall()

    async def delete(self, key: str) -> None:
        blob = self._client.get_blob_client(self._container, _key(key))
        try:
            await blob.delete_blob()
        except ResourceNotFoundError:
            pass

    async def close(self) -> None:
        """Let go of the connection pool. Called from the lifespan's shutdown."""
        await self._client.close()


def _key(key: str) -> str:
    """A key Azure will accept.

    Backslashes are not separators here - a Windows-shaped path would become a
    blob with a backslash in its name, findable by nothing. Leading slashes go
    for the same reason: they make an empty first segment.
    """
    cleaned = key.replace("\\", "/").lstrip("/")
    return cleaned[:MAX_KEY]
