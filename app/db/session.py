"""The only place engines are built, so every engine gets the same safety options."""

from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

# Statement parameters are row values (names, last four digits). With this option an error
# shows the SQL but not the values bound to it.
SAFE_ENGINE_OPTIONS: dict[str, Any] = {"hide_parameters": True}


def create_database_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True, **SAFE_ENGINE_OPTIONS)


def create_sync_engine(settings: Settings, **options: Any) -> Engine:
    """A synchronous engine, for Alembic and for test set-up."""
    return create_engine(settings.database_url, **options, **SAFE_ENGINE_OPTIONS)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
