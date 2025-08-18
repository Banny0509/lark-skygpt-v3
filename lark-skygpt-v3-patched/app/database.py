# app/database.py
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, BigInteger, UniqueConstraint, Index, DateTime, func

from .config import settings

# --- 規範/強化連線字串 ---
url = settings.DATABASE_URL.strip()
# Normalize Railway 的 'postgresql://' → async driver
if url.startswith("postgres://"):
    url = "postgresql+asyncpg://" + url[len("postgres://"):]
elif url.startswith("postgresql://"):
    url = "postgresql+asyncpg://" + url[len("postgresql://"):]

# 追加連線超時避免啟動卡住（可選）
if "connect_timeout=" not in url:
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}connect_timeout=5"

engine = create_async_engine(
    url,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
)
AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


# =========================
#        ORM Models
# =========================
class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chat_id    = Column(String(128), index=True, nullable=True)
    message_id = Column(String(128), unique=True, index=True, nullable=True)

    # 事件時間（毫秒）
    ts_ms      = Column(BigInteger, nullable=True)

    # 關鍵：你寫入用到的兩個欄位
    chat_type  = Column(String(16),  nullable=True)   # p2p / group
    msg_type   = Column(String(32),  nullable=True)   # text / image / file ...

    # 內容/資源鍵
    text       = Column(String,      nullable=True)
    file_key   = Column(String(128), nullable=True)
    image_key  = Column(String(128), nullable=True)

    # 方便查詢：chat_id + ts_ms
    __table_args__ = (
        Index("ix_messages_chat_time", "chat_id", "ts_ms"),
    )


class SummaryLock(Base):
    __tablename__ = "summary_lock"
    summary_date = Column(String, primary_key=True)   # YYYY-MM-DD
    chat_id      = Column(String, primary_key=True)
    __table_args__ = (UniqueConstraint('summary_date', 'chat_id', name='_summary_date_chat_uc'),)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
