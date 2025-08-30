# app/crud.py
from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from .database import Message, SummaryLock, Chat

# ============ Chat 相关 ============
async def upsert_chat(db: AsyncSession, chat_id: str, name: Optional[str]) -> None:
    row = (await db.execute(select(Chat).where(Chat.chat_id == chat_id))).scalar_one_or_none()
    now = datetime.utcnow()
    if row:
        if name:
            row.name = name
        row.last_seen = now
    else:
        db.add(Chat(chat_id=chat_id, name=name, last_seen=now))
    try:
        await db.commit()
    except Exception:
        await db.rollback()

async def set_chat_enabled(db: AsyncSession, chat_id: str, enabled: bool) -> None:
    row = (await db.execute(select(Chat).where(Chat.chat_id == chat_id))).scalar_one_or_none()
    if not row:
        row = Chat(chat_id=chat_id, enabled=enabled)
        db.add(row)
    else:
        row.enabled = enabled
    try:
        await db.commit()
    except Exception:
        await db.rollback()

async def set_chat_schedule(
    db: AsyncSession, chat_id: str, hour: int | None = None, tz: str | None = None, lang: str | None = None
) -> None:
    row = (await db.execute(select(Chat).where(Chat.chat_id == chat_id))).scalar_one_or_none()
    if not row:
        row = Chat(chat_id=chat_id)
        db.add(row)
    if hour is not None:
        row.summary_hour = max(0, min(23, int(hour)))
    if tz:
        row.tz = tz
    if lang:
        row.lang = lang
    try:
        await db.commit()
    except Exception:
        await db.rollback()

async def get_all_chats(db: AsyncSession) -> List[Dict[str, Any]]:
    res = await db.execute(select(Chat).where(Chat.enabled.is_(True)))
    rows = res.scalars().all()
    out = [{"chat_id": r.chat_id, "tz": r.tz, "hour": r.summary_hour, "lang": r.lang, "name": r.name} for r in rows]
    if out:
        return out
    # 允许回退环境变量（应急/灰度）
    import os
    ids_env = (os.getenv("SUMMARY_CHAT_IDS", "") or "").strip()
    if ids_env:
        return [{"chat_id": i.strip(), "tz": "Asia/Taipei", "hour": 8, "lang": "zh", "name": None}
                for i in ids_env.split(",") if i.strip()]
    return []

# ============ Message 相关 ============
async def save_message(
    db: AsyncSession,
    chat_id: str,
    text: str,
    sender_id: str,
    ts_ms: int,
    msg_type: str = "text",
) -> None:
    exists = await db.execute(
        select(Message).where(
            and_(
                Message.chat_id == chat_id,
                Message.ts_ms == ts_ms,
                Message.text == text,
            )
        )
    )
    if exists.scalar_one_or_none():
        return
    m = Message(chat_id=chat_id, ts_ms=ts_ms, text=text, sender_id=sender_id, msg_type=msg_type)
    db.add(m)
    try:
        await db.commit()
    except Exception:
        await db.rollback()

async def get_messages_between(
    db: AsyncSession, chat_id: str, start: datetime, end: datetime
) -> List[Dict[str, Any]]:
    res = await db.execute(
        select(Message).where(
            and_(
                Message.chat_id == chat_id,
                Message.ts_ms >= int(start.timestamp() * 1000),
                Message.ts_ms <  int(end.timestamp() * 1000),
            )
        ).order_by(Message.ts_ms.asc())
    )
    rows = res.scalars().all()
    return [{"text": r.text, "ts_ms": r.ts_ms, "sender_id": r.sender_id, "type": r.msg_type} for r in rows]

# ============ 摘要锁（当日防重）===========
async def acquire_summary_lock(db: AsyncSession, summary_date: str, chat_id: str) -> bool:
    exists = await db.execute(
        select(SummaryLock).where(SummaryLock.summary_date == summary_date, SummaryLock.chat_id == chat_id)
    )
    if exists.scalar_one_or_none():
        return False
    try:
        db.add(SummaryLock(summary_date=summary_date, chat_id=chat_id))
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        return False
