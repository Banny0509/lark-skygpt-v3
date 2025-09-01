# app/database.py
import os
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import (
    Column, Integer, String, BigInteger, UniqueConstraint, Index, Boolean, DateTime
)

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("DB_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL 未设置")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, future=True)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, index=True, nullable=False)
    message_id = Column(String, unique=True, nullable=True)
    sender_id = Column(String, nullable=True)
    ts_ms = Column(BigInteger, index=True)
    msg_type = Column(String, nullable=True)
    text = Column(String, nullable=True)
    __table_args__ = (Index("ix_messages_chat_time", "chat_id", "ts_ms"),)


class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    summary_hour = Column(Integer, default=8)
    tz = Column(String, default="Asia/Taipei")
    lang = Column(String, default="zh")
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("chat_id", name="uc_chats_chat_id"),)


class SummaryLock(Base):
    __tablename__ = "summary_lock"
    summary_date = Column(String, primary_key=True)
    chat_id = Column(String, primary_key=True)
    __table_args__ = (UniqueConstraint("summary_date", "chat_id", name="_summary_date_chat_uc"),)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

