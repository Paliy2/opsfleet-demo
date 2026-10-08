from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opsfleet_agent.core.config import Settings


def create_database(settings: Settings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
