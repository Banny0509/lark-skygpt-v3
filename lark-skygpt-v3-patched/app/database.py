# app/database.py
from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import (
    Column, Integer, String, BigInteger, Text,
    UniqueConstraint, Index,
)

from .config import settings

logger = logging.getLogger(__name__)


# ---------- helpers ----------

def _normalize_driver(dsn: str) -> str:
    """把 postgres / postgresql 前綴換成 asyncpg driver 前綴。"""
    dsn = (dsn or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is empty")

    if dsn.startswith("postgres://"):
        dsn = "postgresql+asyncpg://" + dsn[len("postgres://"):]
    elif dsn.startswith("postgresql://"):
        dsn = "postgresql+asyncpg://" + dsn[len("postgresql://"):]
    return dsn


def _drop_all_query_params(dsn: str) -> str:
    """把 URL 裡的 ?querystring 整個移除，避免 connect_timeout 等參數滲進 asyncpg。"""
    p = urlsplit(dsn)
    # 只保留 scheme / netloc / path；query / fragment 都砍掉
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _mask_dsn(dsn: str) -> str:
    """遮密後輸出，協助在日誌確認最終 DSN（不可含 ?）。"""
    try:
        p = urlsplit(dsn)
        user = p.username or ""
        netloc = f"{user}:***@{p.hostname}:{p.port}" if p.hostname else p.netloc
        return urlunsplit((p.scheme, netloc, p.path, "", ""))
    except Exception:
        return "masked-dsn"


# ---------- build final DSN ----------

raw_url = settings.DATABASE_URL
url = _normalize_driver(raw_url)
url = _drop_all_query_params(url)   # <--- 關鍵：砍掉所有 query 參數

logger.info("[DB] Final DSN (masked): %s", _mask_dsn(url))


# ---------- async engine & session ----------

# 注意：asyncpg 的參數名是 timeout（不是 connect_timeout）
engine = create_async_engine(
    url,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
    connect_args={"timeout": 5},
)

AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


# ---------- ORM models（貼合你現有的資料表結構） ----------

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chat_id = Column(String(128), index=True, nullable=True)
    message_id = Column(String(128), index=True, nullable=True)
    sender_id = Column(String, nullable=True)

    ts_ms = Column(BigInteger, nullable=True)

    chat_type = Column(String(16), nullable=True)   # p2p / group
    msg_type  = Column(String(32), nullable=True)   # text / image / file

    text = Column(Text, nullable=True)
    file_key  = Column(String(128), nullable=True)
    image_key = Column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_messages_chat_time", "chat_id", "ts_ms"),
        # 其他索引（ux_messages_message_id、ix_messages_chat_id、ix_messages_ts_ms）
        # 已用 SQL 腳本建立，避免 ORM 再次建立造成衝突。
    )


class SummaryLock(Base):
    __tablename__ = "summary_lock"

    summary_date = Column(String, primary_key=True)      # YYYY-MM-DD
    chat_id      = Column(String(128), primary_key=True)

    __table_args__ = (
        UniqueConstraint("summary_date", "chat_id", name="_summary_date_chat_uc"),
    )


# ---------- init & dependency ----------

async def init_db() -> None:
    """啟動時若缺表則建立（冪等）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI 依賴注入：提供 AsyncSession。"""
    async with AsyncSessionFactory() as session:
        yield session
