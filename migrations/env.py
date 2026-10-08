import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from opsfleet_agent.core.config import get_settings
from opsfleet_agent.models.tables import Base

target_metadata = Base.metadata


def _configure(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(get_settings().database_url.get_secret_value())
    async with engine.connect() as connection:
        await connection.run_sync(_configure)
    await engine.dispose()


def _run_offline() -> None:
    context.configure(
        url=get_settings().database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    asyncio.run(_run_online())
