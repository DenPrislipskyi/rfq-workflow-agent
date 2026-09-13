"""Translating the service's URL for the driver that runs migrations.

One function, and it earns a file of its own because getting it wrong fails
late and confusingly. The service talks to Postgres through asyncpg; Alembic
is synchronous and talks through psycopg. Swapping the driver is the obvious
half. The half that was missed:

    asyncpg   ?ssl=require
    psycopg   ?sslmode=require

Neither accepts the other's spelling. A URL that worked all the way through
the service died on `alembic upgrade head` against Azure with
`invalid connection option "ssl"`, thrown from inside a connection pool by
something that never mentions Alembic.
"""

from src.core.database import synchronous


def test_the_driver_is_swapped_for_a_synchronous_one():
    assert synchronous("postgresql+asyncpg://u:p@host:5432/db").startswith("postgresql+psycopg://")


def test_asyncpg_s_ssl_becomes_psycopg_s_sslmode():
    """The bug. Azure enforces TLS, so every deployed URL carries this."""
    translated = synchronous("postgresql+asyncpg://u:p@azure:5432/rfq-db?ssl=require")

    assert translated == "postgresql+psycopg://u:p@azure:5432/rfq-db?sslmode=require"


def test_a_url_with_no_query_is_left_alone():
    """The local one. Nothing to translate, and nothing invented."""
    assert (
        synchronous("postgresql+asyncpg://rfq:pw@localhost:5433/rfq")
        == "postgresql+psycopg://rfq:pw@localhost:5433/rfq"
    )


def test_other_parameters_survive_the_translation():
    translated = synchronous("postgresql+asyncpg://u:p@h/db?ssl=require&application_name=rfq")

    assert "application_name=rfq" in translated
    assert "sslmode=require" in translated
    assert "ssl=require" not in translated.replace("sslmode=require", "")


def test_the_two_spellings_asyncpg_takes_for_a_boolean():
    """asyncpg accepts `true`/`false`; psycopg has no such values."""
    assert "sslmode=require" in synchronous("postgresql+asyncpg://u:p@h/db?ssl=true")
    assert "sslmode=disable" in synchronous("postgresql+asyncpg://u:p@h/db?ssl=false")


def test_a_mode_psycopg_already_understands_passes_through():
    for mode in ("require", "prefer", "allow", "verify-ca", "verify-full"):
        assert f"sslmode={mode}" in synchronous(f"postgresql+asyncpg://u:p@h/db?ssl={mode}")


def test_a_password_with_an_at_sign_survives():
    """It reaches here percent-encoded and must stay that way: decoding it
    would put a second `@` in the URL and move the host."""
    translated = synchronous("postgresql+asyncpg://rfqadmin:pw%40word@azure:5432/db?ssl=require")

    assert "rfqadmin:pw%40word@azure" in translated
