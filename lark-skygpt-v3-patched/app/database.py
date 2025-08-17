import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, BigInteger, UniqueConstraint, Index

from .config import settings


url = settings.DATABASE_URL
# Normalize Railway's 'postgresql://' URL to async driver
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(url, pool_pre_ping=True, future=True)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String, index=True, nullable=False)
    message_id = Column(String, unique=True)
    sender_id = Column(String)
    ts_ms = Column(BigInteger, index=True)
    msg_type = Column(String)
    text = Column(String, nullable=True)
    file_key = Column(String, nullable=True)
    image_key = Column(String, nullable=True)
    Index("ix_messages_chat_time", chat_id, ts_ms)

class SummaryLock(Base):
    __tablename__ = "summary_lock"
    summary_date = Column(String, primary_key=True)   # YYYY-MM-DD
    chat_id = Column(String, primary_key=True)
    __table_args__ = (UniqueConstraint('summary_date', 'chat_id', name='_summary_date_chat_uc'),)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
