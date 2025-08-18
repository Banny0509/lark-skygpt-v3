from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .database import Message

async def insert_message(
    session: AsyncSession,
    *,
    chat_id: str | None,
    message_id: str | None,
    ts_ms: int | None,
    chat_type: str | None,
    msg_type: str | None,
    text: str | None = None,
    file_key: str | None = None,
    image_key: str | None = None,
    sender_id: str | None = None,
) -> Message:
    m = Message(
        chat_id=chat_id,
        message_id=message_id,
        ts_ms=ts_ms,
        chat_type=chat_type,
        msg_type=msg_type,
        text=text,
        file_key=file_key,
        image_key=image_key,
        sender_id=sender_id,
    )
    session.add(m)
    try:
        await session.commit()
    except Exception:
        await session.rollback()  # 忽略重覆 message_id 的衝突
    return m

async def get_message_by_id(session: AsyncSession, message_id: str) -> Message | None:
    q = await session.execute(select(Message).where(Message.message_id == message_id))
    return q.scalar_one_or_none()
