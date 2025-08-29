# app/database.py
import os
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import (
    Column, Integer, String, BigInteger, UniqueConstraint, Index, Boolean, DateTime
)

from .config import settings

# ------ DB URL 规范化（兼容 Railway 的 postgresql:// → async 驱动）------
url = settings.DATABASE_URL
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(url, pool_pre_ping=True, future=True)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# =========================
# 消息表（用于摘要）
# =========================
class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, index=True, nullable=False)
    message_id = Column(String, unique=True, nullable=True)   # 若能拿到，优先用于去重
    sender_id = Column(String, nullable=True)
    ts_ms = Column(BigInteger, index=True)                    # 毫秒时间戳
    msg_type = Column(String, nullable=True)
    text = Column(String, nullable=True)
    file_key = Column(String, nullable=True)
    image_key = Column(String, nullable=True)

    __table_args__ = (
        Index("ix_messages_chat_time", "chat_id", "ts_ms"),
    )

# =========================
# 群表（入群即登记 & 每日摘要配置）
# =========================
class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)

    enabled = Column(Boolean, default=True)
    summary_hour = Column(Integer, default=8)                 # 0~23
    tz = Column(String, default="Asia/Taipei")
    lang = Column(String, default="zh")

    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("chat_id", name="uc_chats_chat_id"),
    )

# =========================
# 当日防重锁（同日同群仅跑一次）
# =========================
class SummaryLock(Base):
    __tablename__ = "summary_lock"
    summary_date = Column(String, primary_key=True)  # 例如 "2025-08-29"
    chat_id = Column(String, primary_key=True)
    __table_args__ = (UniqueConstraint("summary_date", "chat_id", name="_summary_date_chat_uc"),)

# =========================
# 初始化建表
# =========================
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
