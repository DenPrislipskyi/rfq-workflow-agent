class GraphError(Exception):
    """Base error for every Microsoft Graph interaction."""


class GraphAuthError(GraphError):
    """Raised when an access token cannot be acquired from Entra ID."""


class GraphAPIError(GraphError):
    """Raised when Microsoft Graph answers with a non-successful status."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Graph API error {status_code}: {detail}")
