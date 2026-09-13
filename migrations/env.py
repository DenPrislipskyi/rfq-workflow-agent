from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from src.core.config import get_settings
from src.core.database import Base  # noqa: F401
import src.infrastructure.db  # noqa: F401  imported for its side effect: the models

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Every table `src.infrastructure.db` defined by being imported above.
# Autogenerate diffs the database against this and nothing else, so a model in
# a module nobody imports is a model Alembic will happily drop.
target_metadata = Base.metadata

# The placeholder alembic.ini ships with. Anything else means a caller set the
# URL deliberately - `get_alembic_config` does - and that one wins.
PLACEHOLDER = "driver://user:pass@localhost/dbname"


def get_url() -> str:
    """The database to migrate, with a driver that can do it synchronously.

    Alembic's machinery is synchronous, so the `+asyncpg` the service connects
    with is swapped for `+psycopg`. Same server, same database, different door.

    Read from settings rather than from alembic.ini so that `alembic upgrade
    head` works from a shell with nothing but `.env` - and so that the URL
    lives in exactly one place. A caller that set it explicitly is honoured.
    """
    configured = config.get_main_option("sqlalchemy.url", "")
    if configured and configured != PLACEHOLDER:
        return configured

    url = get_settings().DATABASE_URL.unicode_string()
    return url.replace("postgresql+asyncpg", "postgresql+psycopg")


def run_migrations_offline() -> None:
    """Emit SQL rather than run it: `alembic upgrade head --sql`.

    Needs no driver and no database, which is what makes it useful for handing
    a DBA the statements before they touch anything.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run. The usual path."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        # A migration opens one connection, uses it and leaves. Pooling here
        # would hold a connection open after the process is done with it.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Off by default, and its absence is how a column silently stays
            # VARCHAR(64) after the model says VARCHAR(256).
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """What autogenerate is allowed to notice.

    Everything, for now. The hook is here because the moment something else
    shares this database - an extension's tables, a reporting view - the
    alternative is a migration that proposes dropping it.
    """
    return True


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
