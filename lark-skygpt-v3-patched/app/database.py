from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, BigInteger, Text, UniqueConstraint, Index

from .config import settings

logger = logging.getLogger(__name__)

def _normalize_driver(dsn: str) -> str:
    dsn = (dsn or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is empty")
    if dsn.startswith("postgres://"):
        dsn = "postgresql+asyncpg://" + dsn[len("postgres://"):]
    elif dsn.startswith("postgresql://"):
        dsn = "postgresql+asyncpg://" + dsn[len("postgresql://"):]
    return dsn

def _drop_all_query_params(dsn: str) -> str:
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))

def _mask(dsn: str) -> str:
    try:
        p = urlsplit(dsn)
        user = p.username or ""
        netloc = f"{user}:***@{p.hostname}:{p.port}" if p.hostname else p.netloc
        return urlunsplit((p.scheme, netloc, p.path, "", ""))
    except Exception:
        return "masked"

raw = settings.DATABASE_URL
url = _drop_all_query_params(_normalize_driver(raw))
logger.info("[DB] Final DSN: %s", _mask(url))

engine = create_async_engine(
    url,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
    connect_args={"timeout": 5},  # asyncpg 正確參數名
)

AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# ---- ORM 與你現有表結構一致 ----
class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(String(128), index=True, nullable=True)
    message_id = Column(String(128), index=True, nullable=True)
    sender_id = Column(String, nullable=True)
    ts_ms = Column(BigInteger, nullable=True)
    chat_type = Column(String(16), nullable=True)   # p2p/group
    msg_type  = Column(String(32), nullable=True)   # text/image/file
    text = Column(Text, nullable=True)
    file_key  = Column(String(128), nullable=True)
    image_key = Column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_messages_chat_time", "chat_id", "ts_ms"),
    )


class SummaryLock(Base):
    __tablename__ = "summary_lock"
    summary_date = Column(String, primary_key=True)
    chat_id      = Column(String(128), primary_key=True)
    __table_args__ = (
        UniqueConstraint("summary_date", "chat_id", name="_summary_date_chat_uc"),
    )

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionFactory() as s:
        yield s
