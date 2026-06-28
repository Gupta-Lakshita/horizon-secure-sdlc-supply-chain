import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Use SQLite locally, PostgreSQL in production
_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./release_trust.db")

# Normalise postgres URLs → keep as-is in prod, SQLite wins locally
if not _url.startswith("sqlite"):
    if _url.startswith("postgresql://"):
        _url = _url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif _url.startswith("postgres://"):
        _url = _url.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session