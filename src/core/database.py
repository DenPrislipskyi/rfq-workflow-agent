"""The engine, the session and the declarative base.

Lives in `core` because it is wiring, not logic: the models that use `Base` are
infrastructure, and nothing in `domain` knows a database exists at all.

Two things are deliberate and worth keeping:

    every table carries `created_at` and `updated_at`, from `Base`
    Alembic runs synchronously, on `psycopg`, while the service runs on asyncpg

The second is not an inconsistency. A migration is a short, ordered, blocking
job, and running it through an event loop buys nothing and costs a class of
confusing failures.
"""

from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic.config import Config
from pydantic import PostgresDsn
from sqlalchemy import TIMESTAMP, MetaData, func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.config import get_settings

# Constraint names are generated rather than left to Postgres, because
# `alembic downgrade` has to be able to name what it is dropping. Without this,
# an index created by one machine and dropped by another is a coin toss.
POSTGRES_INDEXES_NAMING_CONVENTION = {
    "ix": "%(column_0_label)s_idx",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    """Every table, with the two timestamps every table wants.

    `server_default=func.now()` rather than a Python default: a row written by
    a migration, a script or psql gets the same treatment as one written by the
    service, and the clock that matters is the database's.
    """

    __abstract__ = True
    metadata = MetaData(naming_convention=POSTGRES_INDEXES_NAMING_CONVENTION)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )


@lru_cache
def _async_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.DATABASE_URL.unicode_string(),
        # A Burstable server drops idle connections, and a pooled connection
        # that died quietly surfaces as a failed webhook an hour later.
        pool_pre_ping=True,
        pool_size=settings.DB_POOL_SIZE,
    )


@lru_cache
def _async_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=_async_engine(),
        autoflush=False,
        # The handler reads fields off a record after the session closes, and
        # expiring them would turn every read into a query against a connection
        # that is already back in the pool.
        expire_on_commit=False,
    )


async def get_session() -> AsyncIterable[AsyncSession]:
    """One session per request, for `Depends`."""
    async with open_db_session() as session:
        yield session


@asynccontextmanager
async def open_db_session() -> AsyncGenerator[AsyncSession]:
    """The same session outside FastAPI: tools, the webhook handler, tests.

    Commits on the way out and rolls back on the way out through an exception,
    so no caller has to remember which of the two it was.
    """
    factory = _async_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        await session.close()


# The same setting, spelled differently by the two drivers. asyncpg takes
# `ssl`, psycopg and psql take `sslmode`, and neither accepts the other's:
# psycopg answers `invalid connection option "ssl"` and stops there.
SSL_VALUES = {"true": "require", "false": "disable", "": "require"}


def synchronous(url: str) -> str:
    """The same database, described for a synchronous driver.

    Alembic's machinery is synchronous, so a URL written for the service has to
    be translated before it can be used to migrate. Two things change and both
    matter:

        postgresql+asyncpg  ->  postgresql+psycopg
        ?ssl=require        ->  ?sslmode=require

    The second is easy to miss and fails late: the driver swap alone leaves a
    query parameter psycopg refuses, and the error arrives from inside the
    connection pool rather than from anything that mentions Alembic.
    """
    url = url.replace("postgresql+asyncpg", "postgresql+psycopg")

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "ssl" for key, _ in query):
        return url

    translated = [
        ("sslmode", SSL_VALUES.get(value.lower(), value)) if key == "ssl" else (key, value)
        for key, value in query
    ]
    return urlunsplit(parts._replace(query=urlencode(translated)))


def get_alembic_config(
    database_url: PostgresDsn, script_location: str = "migrations"
) -> Config:
    """Alembic pointed at one database, for running migrations from code."""
    alembic_config = Config()
    alembic_config.set_main_option("script_location", script_location)
    alembic_config.set_main_option("sqlalchemy.url", synchronous(database_url.unicode_string()))
    return alembic_config
