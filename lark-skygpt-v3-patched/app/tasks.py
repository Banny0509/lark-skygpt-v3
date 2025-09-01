"""
Asynchronous tasks for message recording, summarisation and command handling.
"""

from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime, timedelta, time as dt_time
from typing import Dict, Any

import httpx
from zoneinfo import ZoneInfo

from .openai_client import summarize_text_or_fallback
from .database import AsyncSessionFactory
from . import crud

# 尝试从 main 导入 reply_text；失败时留空
try:
    from .main import reply_text  # type: ignore
except Exception:
    reply_text = None  # type: ignore

logger = logging.getLogger("worker")
logging.basicConfig(level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO))

DEFAULT_TZ = ZoneInfo("Asia/Taipei")


async def _say(http: httpx.AsyncClient, chat_id: str, text: str) -> None:
    try:
        if reply_text:
            await reply_text(http, chat_id, text)  # type: ignore
            logger.info("[_say] reply_text OK chat_id=%s len(text)=%s", chat_id, len(text))
            return
    except Exception as e:
        logger.debug("[_say] reply_text failed: %s", e)

    try:
        from . import lark_client
        if hasattr(lark_client, "send_text_to_chat"):
            await lark_client.send_text_to_chat(http, chat_id, text)
            logger.info("[_say] send_text_to_chat OK chat_id=%s", chat_id)
            return
    except Exception as e:
        logger.warning("[_say] fallback failed: %s", e)


def _yesterday_range(tz: str | ZoneInfo = DEFAULT_TZ) -> tuple[datetime, datetime]:
    z = ZoneInfo(str(tz)) if isinstance(tz, str) else tz
    now = datetime.now(z)
    today = now.date()
    y = today - timedelta(days=1)
    start = datetime.combine(y, dt_time.min, tzinfo=z)
    end = datetime.combine(today, dt_time.min, tzinfo=z)
    return start, end


# ============ 记录消息 ============
async def record_message(event: Dict[str, Any]) -> None:
    try:
        logger.info("[record_message] triggered")
        ev = event.get("event", {}) or {}
        msg = ev.get("message", {}) or {}
        chat_id = msg.get("chat_id")
        msg_type = (msg.get("message_type") or "text").lower()
        logger.info("[record_message] chat_id=%s, msg_type=%s", chat_id, msg_type)

        if not chat_id:
            logger.info("[record_message] missing chat_id, skip")
            return

        raw_content = msg.get("content") or "{}"
        try:
            content = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
        except Exception:
            content = {}
        text = (content.get("text") or "").strip() if isinstance(content, dict) else ""

        # 发送者
        sender_id = ""
        sender = msg.get("sender") or {}
        if isinstance(sender, dict):
            sid = sender.get("id")
            if isinstance(sid, dict):
                sender_id = sid.get("open_id", "") or sid.get("user_id", "") or ""
            else:
                sender_id = sender.get("sender_id") or sender.get("open_id") or (sid if isinstance(sid, str) else "")

        # 时间戳
        try:
            ts_ms = int(msg.get("create_time") or 0)
        except Exception:
            ts_ms = int(datetime.utcnow().timestamp() * 1000)

        # 兜底：summary 指令直达
        if text.lower().startswith("#summary"):
            logger.info("[record_message] fallback summary command -> %s", text)
            try:
                await maybe_handle_summary_command({"event": {"message": {"chat_id": chat_id, "content": json.dumps({"text": text})}}})
            except Exception as e:
                logger.debug("[record_message] fallback maybe_handle_summary_command failed: %s", e)
            return

        if msg_type == "text" and text:
            async with AsyncSessionFactory() as db:
                await crud.upsert_chat(db, chat_id, None)
                logger.info("[record_message] upsert_chat ok chat_id=%s", chat_id)
                await crud.save_message(db, chat_id=chat_id, text=text, sender_id=sender_id, ts_ms=ts_ms, msg_type="text")
                logger.info("[record_message] save_message ok len(text)=%s", len(text))
    except Exception as e:
        logger.debug("record_message failed: %s", e)


# ====== 群管理命令：#summary on/off/at/tz/lang/once ============
async def maybe_handle_summary_command(event: Dict[str, Any]) -> None:
    ev = event.get("event", {}) or {}
    msg = ev.get("message", {}) or {}
    chat_id = msg.get("chat_id")
    raw_content = msg.get("content", "{}")

    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
    except Exception:
        content = {}
    text = ((content.get("text") or "") if isinstance(content, dict) else "").strip().lower()
    logger.info("[maybe] chat_id=%s text=%s", chat_id, text)

    if not chat_id or not text:
        logger.info("[maybe] missing chat_id or empty text")
        return

    async with AsyncSessionFactory() as db:
        await crud.upsert_chat(db, chat_id, None)
        logger.info("[maybe] upsert_chat ok chat_id=%s", chat_id)

        if "#summary on" in text:
            await crud.set_chat_enabled(db, chat_id, True)
            async with httpx.AsyncClient() as http:
                await _say(http, chat_id, "已开启本群每日摘要。")
            logger.info("[maybe] set enabled True")
            return

        if "#summary off" in text:
            await crud.set_chat_enabled(db, chat_id, False)
            async with httpx.AsyncClient() as http:
                await _say(http, chat_id, "已关闭本群每日摘要。")
            logger.info("[maybe] set enabled False")
            return

        m = re.search(r"#summary\s+at\s+(\d{1,2})(?::\d{2})?", text)
        if m:
            hour = max(0, min(23, int(m.group(1))))
            await crud.set_chat_schedule(db, chat_id, hour=hour)
            async with httpx.AsyncClient() as http:
                await _say(http, chat_id, f"已更新本群每日摘要时间为 {hour:02d}:00。")
            logger.info("[maybe] set hour=%s", hour)
            return

        m = re.search(r"#summary\s+tz\s+([\w/\-]+)", text)
        if m:
            tz = m.group(1)
            await crud.set_chat_schedule(db, chat_id, tz=tz)
            async with httpx.AsyncClient() as http:
                await _say(http, chat_id, f"已更新本群摘要时区为 {tz}。")
            logger.info("[maybe] set tz=%s", tz)
            return

        m = re.search(r"#summary\s+lang\s+(zh|en)", text)
        if m:
            lang = m.group(1)
            await crud.set_chat_schedule(db, chat_id, lang=lang)
            async with httpx.AsyncClient() as http:
                await _say(http, chat_id, f"已更新本群摘要语言为 {lang}。")
            logger.info("[maybe] set lang=%s", lang)
            return

        if "#summary once" in text:
            async with httpx.AsyncClient() as http:
                await summarize_for_single_chat(http, chat_id)
            logger.info("[maybe] trigger once summary")
            return


# ============ 单群摘要 ============
async def summarize_for_single_chat(http: httpx.AsyncClient, chat_id: str, tz: str | ZoneInfo = DEFAULT_TZ) -> None:
    start, end = _yesterday_range(tz)
    async with AsyncSessionFactory() as db:
        msgs = await crud.get_messages_between(db, chat_id, start, end)

    if not msgs:
        await _say(http, chat_id, f"（提示）{start.date()} 无聊天记录，略过摘要。")
        logger.info("[summarize one] no messages chat_id=%s", chat_id)
        return

    text = "\n".join(f"[{i+1}] {m.get('text','')}" for i, m in enumerate(msgs) if (m.get("text") or "").strip())
    try:
        summary = await summarize_text_or_fallback(http, text)
    except Exception as e:
        logger.exception("[summarize one] summarize failed: %s", e)
        summary = "(降级) 摘要服务暂不可用。"

    await _say(http, chat_id, f"【{start.date()} 日聊摘】\n{summary}")
    logger.info("[summarize one] done chat_id=%s", chat_id)


# ============ 全量摘要 ============
async def summarize_for_all_chats(http: httpx.AsyncClient) -> None:
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
    if not chats:
        logger.info("[summarize all] no active chats")
        return

    for c in chats:
        tz = c.get("tz") or "Asia/Taipei"
        chat_id = c.get("chat_id")
        if not chat_id:
            continue
        try:
            await summarize_for_single_chat(http, chat_id, tz=tz)
        except Exception as e:
            logger.exception("[summarize all] chat=%s error=%s", chat_id, e)

