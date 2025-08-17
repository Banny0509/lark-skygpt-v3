from typing import Dict, Any, List
from sqlalchemy import select, distinct
from sqlalchemy.ext.asyncio import AsyncSession
from .database import Message, SummaryLock

async def insert_message(db: AsyncSession, row: Dict[str, Any]):
    m = Message(**row)
    db.add(m)
    try:
        await db.commit()
    except Exception:
        await db.rollback()

async def get_chat_ids_with_messages_between(db: AsyncSession, start_ms: int, end_ms: int) -> List[str]:
    q = select(distinct(Message.chat_id)).where(Message.ts_ms >= start_ms, Message.ts_ms < end_ms)
    res = await db.execute(q)
    return [r[0] for r in res.all()]

async def acquire_summary_lock(db: AsyncSession, date_str: str, chat_id: str) -> bool:
    lock = SummaryLock(summary_date=date_str, chat_id=chat_id)
    db.add(lock)
    try:
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        return False
