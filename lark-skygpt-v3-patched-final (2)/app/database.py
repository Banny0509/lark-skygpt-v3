# app/database.py
from __future__ import annotations

from typing import Iterable, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import (
    Column,
    Integer,
    String,
    BigInteger,
    Text,
    UniqueConstraint,
    Index,
)

from .config import settings


# ------------------------------
# Helpers
# ------------------------------
def _normalize_driver(dsn: str) -> str:
    """Convert sync postgres DSN to asyncpg driver DSN."""
    dsn = (dsn or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is empty")
    if dsn.startswith("postgres://"):
        return "postgresql+asyncpg://" + dsn[len("postgres://") :]
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    # already with driver?
    return dsn


def _strip_bad_query_params(dsn: str, banned: Iterable[str]) -> str:
    """Remove query params that asyncpg doesn't accept (e.g., connect_timeout)."""
    parts = urlsplit(dsn)
    q: list[Tuple[str, str]] = parse_qsl(parts.query, keep_blank_values=True)
    banned_lc = {k.lower() for k in banned}
    q2 = [(k, v) for (k, v) in q if k.lower() not in banned_lc]
    if q2 == q:
        return dsn  # nothing to change
    new_query = urlencode(q2, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


# ------------------------------
# Build final DSN
# ------------------------------
raw_url = settings.DATABASE_URL
url = _normalize_driver(raw_url)
# Remove problematic params injected via env, e.g. ?connect_timeout=5
url = _strip_bad_query_params(url, banned=["connect_timeout"])

# ------------------------------
# Async engine & session factory
# ------------------------------
# NOTE:
# - asyncpg 參數名為 `timeout`（不是 connect_timeout）
# - 若你真的需要 SSL，可在 connect_args 傳 ssl.SSLContext
engine = create_async_engine(
    url,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
    connect_args={"timeout": 5},
)

AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


# ------------------------------
# ORM Models（與既有 DB 結構相容）
# ------------------------------
class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chat_id = Column(String(128), index=True, nullable=True)
    message_id = Column(String(128), index=True, nullable=True)
    sender_id = Column(String, nullable=True)

    ts_ms = Column(BigInteger, nullable=True)

    chat_type = Column(String(16), nullable=True)   # p2p / group
    msg_type = Column(String(32), nullable=True)    # text / image / file

    text = Column(Text, nullable=True)
    file_key = Column(String(128), nullable=True)
    image_key = Column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_messages_chat_time", "chat_id", "ts_ms"),
        # 其他索引（ux_messages_message_id、ix_messages_chat_id、ix_messages_ts_ms）
        # 已用 SQL 腳本建立，避免 ORM 重複建約束。
    )


class SummaryLock(Base):
    __tablename__ = "summary_lock"

    summary_date = Column(String, primary_key=True)  # YYYY-MM-DD
    chat_id = Column(String(128), primary_key=True)

    __table_args__ = (
        UniqueConstraint("summary_date", "chat_id", name="_summary_date_chat_uc"),
    )


# ------------------------------
# Init / Dependency
# ------------------------------
async def init_db() -> None:
    """Create tables on startup if missing (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionFactory() as session:
        yield session
