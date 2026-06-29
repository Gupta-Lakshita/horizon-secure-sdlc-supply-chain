import os
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, Session

_raw_url = os.getenv("DATABASE_URL", "sqlite:///./release_trust.db")

# Async engine (asyncpg dialect) — used by most API modules
_async_url = _raw_url
if _async_url.startswith("postgresql://"):
    _async_url = _async_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _async_url.startswith("postgres://"):
    _async_url = _async_url.replace("postgres://", "postgresql+asyncpg://", 1)

engine_async = create_async_engine(_async_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine_async, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# Sync engine — used by modules that require sync SQLAlchemy (e.g. exceptions.py)
_sync_url = _raw_url
if _sync_url.startswith("postgresql+asyncpg://"):
    _sync_url = _sync_url.replace("postgresql+asyncpg://", "postgresql://", 1)
elif _sync_url.startswith("postgres://"):
    _sync_url = _sync_url.replace("postgres://", "postgresql://", 1)

engine_sync = create_engine(_sync_url, echo=False, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(engine_sync, class_=Session, expire_on_commit=False)


def get_db_sync() -> Session:
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
