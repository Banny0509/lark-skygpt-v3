# app/tasks.py
import os
import re
import json
import logging
import httpx
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from .openai_client import summarize_text_or_fallback
from .database import AsyncSessionFactory
from . import crud

logger = logging.getLogger(__name__)
DEFAULT_TZ = os.getenv("TZ", "Asia/Taipei")

# 发送消息复用 main.reply_text（你现有实现）
try:
    from .main import reply_text
except Exception:
    reply_text = None  # type: ignore

def _yesterday_range(tz: str = DEFAULT_TZ) -> tuple[datetime, datetime]:
    z = ZoneInfo(tz)
    now = datetime.now(z)
    today = now.date()
    y = today - timedelta(days=1)
    start = datetime.combine(y, dt_time.min, tzinfo=z)       # 昨日 00:00（含）
    end   = datetime.combine(today, dt_time.min, tzinfo=z)   # 今日 00:00（不含）
    return start, end

# ============ 记录消息：由 webhook 异步触发 ============
async def record_message(event: dict) -> None:
    try:
        ev = event.get("event", {}) or {}
        msg = ev.get("message", {}) or {}
        if not msg:
            return
        chat_id = msg.get("chat_id")
        if not chat_id:
            return
        msg_type = msg.get("message_type") or "text"
        content_raw = msg.get("content") or "{}"
        sender = (msg.get("sender") or {}).get("sender_id") or ""
        ts_ms = int(msg.get("create_time") or 0)

        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
        except Exception:
            content = {}
        text = (content.get("text") or "").strip() if isinstance(content, dict) else ""

        if msg_type == "text" and text:
            async with AsyncSessionFactory() as db:
                await crud.save_message(db, chat_id=chat_id, text=text, sender_id=sender, ts_ms=ts_ms, msg_type="text")
                                        await crud.upsert_chat(db, chat_id, None)  
    except Exception as e:
        logger.debug("record_message failed: %s", e)

# ============ 群管理命令：#summary on/off/at/tz/lang/once ============
async def maybe_handle_summary_command(event: dict) -> None:
    ev = event.get("event", {}) or {}
    msg = ev.get("message", {}) or {}
    chat_id = msg.get("chat_id")
    if not chat_id:
        return

    content_raw = msg.get("content", "{}")
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
    except Exception:
        content = {}
    text = ((content.get("text") or "") if isinstance(content, dict) else "").strip().lower()
    if not text:
        return

    async with AsyncSessionFactory() as db:
        if "#summary on" in text:
            await crud.set_chat_enabled(db, chat_id, True)
            async with httpx.AsyncClient() as http:
                if reply_text: await reply_text(http, chat_id, "已开启本群每日摘要。")
            return
        if "#summary off" in text:
            await crud.set_chat_enabled(db, chat_id, False)
            async with httpx.AsyncClient() as http:
                if reply_text: await reply_text(http, chat_id, "已关闭本群每日摘要。")
            return

        m = re.search(r"#summary\s+at\s+(\d{1,2})(?::\d{2})?", text)
        if m:
            hour = max(0, min(23, int(m.group(1))))
            await crud.set_chat_schedule(db, chat_id, hour=hour)
            async with httpx.AsyncClient() as http:
                if reply_text: await reply_text(http, chat_id, f"已更新本群每日摘要时间为 {hour:02d}:00。")
            return

        m = re.search(r"#summary\s+tz\s+([\w/\-]+)", text)
        if m:
            tz = m.group(1)
            await crud.set_chat_schedule(db, chat_id, tz=tz)
            async with httpx.AsyncClient() as http:
                if reply_text: await reply_text(http, chat_id, f"已更新本群摘要时区为 {tz}。")
            return

        m = re.search(r"#summary\s+lang\s+(zh|en)", text)
        if m:
            lang = m.group(1)
            await crud.set_chat_schedule(db, chat_id, lang=lang)
            async with httpx.AsyncClient() as http:
                if reply_text: await reply_text(http, chat_id, f"已更新本群摘要语言为 {lang}。")
            return

        if "#summary once" in text:
            async with httpx.AsyncClient() as http:
                await summarize_for_single_chat(http, chat_id)
            return

# ============ 单群摘要 ============
async def summarize_for_single_chat(http: httpx.AsyncClient, chat_id: str, tz: str = DEFAULT_TZ) -> None:
    start, end = _yesterday_range(tz)
    async with AsyncSessionFactory() as db:
        msgs = await crud.get_messages_between(db, chat_id, start, end)
    if not msgs:
        if reply_text: await reply_text(http, chat_id, f"（提示）{start.date()} 无聊天记录，略过摘要。")
        return

    text = "\n".join(f"[{i+1}] {m.get('text','')}" for i, m in enumerate(msgs) if m.get("text"))
    try:
        summary = await summarize_text_or_fallback(http, text)
    except Exception as e:
        logger.exception("summarize failed: %s", e)
        summary = "(降级) 摘要服务暂不可用。"

    if reply_text: await reply_text(http, chat_id, f"【{start.date()} 日聊摘】\n{summary}")

# ============ 全量摘要：被调度器调用 ============
async def summarize_for_all_chats(http: httpx.AsyncClient) -> None:
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
    if not chats:
        logger.info("no active chats for summary")
        return
    for c in chats:
        tz = c.get("tz") or DEFAULT_TZ
        try:
            await summarize_for_single_chat(http, c["chat_id"], tz=tz)
        except Exception as e:
            logger.exception("chat %s summary error: %s", c["chat_id"], e)

