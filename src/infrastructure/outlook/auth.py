import asyncio

from msal import ConfidentialClientApplication

from src.infrastructure.outlook.exceptions import GraphAuthError

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


class GraphTokenProvider:
    """Client credentials flow: the service authenticates as itself, no user.

    MSAL keeps its own in-memory cache, so Entra ID is only called when no valid
    token is left.
    """

    def __init__(self, client_id: str, client_secret: str, authority: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._authority = authority
        self._client: ConfidentialClientApplication | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        # The thread keeps the blocking MSAL call off the event loop; the lock
        # prevents parallel refreshes and duplicate clients.
        async with self._lock:
            result = await asyncio.to_thread(self._acquire_token)

        token = result.get("access_token")
        if not token:
            raise GraphAuthError(
                result.get("error_description") or "Could not acquire a Graph token"
            )

        return token

    def _acquire_token(self) -> dict:
        if self._client is None:
            self._client = ConfidentialClientApplication(
                client_id=self._client_id,
                client_credential=self._client_secret,
                authority=self._authority,
            )

        return self._client.acquire_token_for_client(scopes=[GRAPH_DEFAULT_SCOPE])
