import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

_url = os.getenv("DATABASE_URL", "sqlite:///./release_trust.db")

if _url.startswith("postgresql://"):
    _url = _url.replace("postgresql://", "postgresql+psycopg2://", 1)
elif _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql+psycopg2://", 1)

engine = create_engine(_url, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()