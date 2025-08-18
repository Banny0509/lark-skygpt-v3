# app/database.py
from __future__ import annotations

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
# Build asyncpg URL (normalize)
# ------------------------------
def _normalize_db_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is empty")

    # Convert sync driver DSN → asyncpg
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


DB_URL = _normalize_db_url(settings.DATABASE_URL)

# ------------------------------
# Async engine & session factory
# ------------------------------
# NOTE:
# - asyncpg 的參數名稱是 `timeout`，不是 connect_timeout
# - Railway 內網 DB 通常不強制 SSL；若需要可在 connect_args 傳 ssl context
engine = create_async_engine(
    DB_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
    connect_args={"timeout": 5},  # 正確的 asyncpg 連線超時參數
)

AsyncSessionFactory = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

Base = declarative_base()

# ------------------------------
# ORM Models
# ------------------------------
class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 會話/訊息識別
    chat_id = Column(String(128), index=True, nullable=True)
    message_id = Column(String(128), index=True, nullable=True)  # DB 內已建唯一索引，這裡只設 index 避免重複建約束
    sender_id = Column(String, nullable=True)

    # 時間（毫秒）
    ts_ms = Column(BigInteger, nullable=True)

    # 來源與型別
    chat_type = Column(String(16), nullable=True)   # p2p / group
    msg_type = Column(String(32), nullable=True)    # text / image / file ...

    # 內容與資源鍵
    text = Column(Text, nullable=True)
    file_key = Column(String(128), nullable=True)
    image_key = Column(String(128), nullable=True)

    # 常用查詢索引：chat_id + ts_ms（對應你的資料庫已有的 ix_messages_chat_time）
    __table_args__ = (
        Index("ix_messages_chat_time", "chat_id", "ts_ms"),
        # 其他索引（如 ix_messages_chat_id / ix_messages_ts_ms / ux_messages_message_id）
        # 已由 SQL 腳本建立；不在 ORM 再宣告以避免 create_all 時嘗試重建。
    )


class SummaryLock(Base):
    __tablename__ = "summary_lock"

    summary_date = Column(String, primary_key=True)   # YYYY-MM-DD
    chat_id = Column(String(128), primary_key=True)

    __table_args__ = (
        UniqueConstraint("summary_date", "chat_id", name="_summary_date_chat_uc"),
    )


# ------------------------------
# DB init (called at startup)
# ------------------------------
async def init_db() -> None:
    """Create tables if not exist. Safe to call on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ------------------------------
# FastAPI dependency helper
# ------------------------------
async def get_db():
    """Yield an AsyncSession (FastAPI dependency style)."""
    async with AsyncSessionFactory() as session:
        yield session
