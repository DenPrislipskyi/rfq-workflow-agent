"""Whether a browser on another host may call this API.

The RFQ screens are a static site on one domain; this is on another. A browser
will not let the first talk to the second unless the second says its origin out
loud, and the failure is quiet from the server's side: the preflight arrives as
`OPTIONS /api/v1/quotes`, no middleware answers it, the router says
`405 Method Not Allowed`, and nothing in that mentions CORS at all.

That is exactly what the deployed agent did, so both halves are pinned here.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from src.main import create_app
from tests.fakes import fake_settings

PAGE = "https://gray-cliff-0b1cd8403.5.azurestaticapps.net"
SOMEBODY_ELSE = "https://not-ours.example.invalid"


@pytest.fixture
def app_with(monkeypatch):
    """An app built with the settings a deployment would have."""

    def build(origins: str):
        from src.core import config

        monkeypatch.setattr(
            config, "get_settings", lambda: fake_settings(CORS_ORIGINS=origins)
        )
        monkeypatch.setattr("src.main.get_settings", lambda: fake_settings(CORS_ORIGINS=origins))
        # `configure_logging` calls `basicConfig(force=True)`, which throws away
        # every handler on the root logger - caplog's included. Left in place,
        # the log assertion below would fail on a message it can see in stderr.
        monkeypatch.setattr("src.main.configure_logging", lambda *_: None)
        return TestClient(create_app())

    return build


def test_a_named_origin_gets_its_preflight_answered(app_with):
    client = app_with(PAGE)

    response = client.options(
        "/api/v1/quotes",
        headers={
            "Origin": PAGE,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PAGE


def test_the_answer_itself_names_the_origin(app_with):
    """A preflight that passes is only half of it: the browser drops the real
    response too unless it carries the header.

    Asked of `/health-check` rather than of the list, because the middleware
    runs before routing and needs no application state to prove the point."""
    client = app_with(PAGE)

    response = client.get("/health-check", headers={"Origin": PAGE})

    assert response.headers.get("access-control-allow-origin") == PAGE


def test_an_origin_nobody_named_is_not_answered(app_with):
    client = app_with(PAGE)

    response = client.get("/health-check", headers={"Origin": SOMEBODY_ELSE})

    assert "access-control-allow-origin" not in response.headers


def test_several_origins_can_be_listed(app_with):
    """A deployed page and a developer's dev server, at the same time."""
    client = app_with(f"{PAGE}, http://localhost:5173")

    for origin in (PAGE, "http://localhost:5173"):
        response = client.get("/health-check", headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") == origin


def test_with_nothing_configured_a_preflight_is_refused(app_with):
    """The deployed failure, pinned. 405 and no header - which is what the
    container was answering while CORS_ORIGINS was unset."""
    client = app_with("")

    preflight = client.options(
        "/api/v1/quotes",
        headers={"Origin": PAGE, "Access-Control-Request-Method": "GET"},
    )

    assert preflight.status_code == 405
    assert "access-control-allow-origin" not in preflight.headers


def test_an_empty_setting_says_so_in_the_log(app_with, caplog):
    """Because 405 names neither CORS nor the setting that would fix it, and a
    log that stays silent sends somebody to read the router."""
    with caplog.at_level(logging.WARNING, logger="src.main"):
        app_with("")

    assert any("CORS_ORIGINS is empty" in record.message for record in caplog.records)
