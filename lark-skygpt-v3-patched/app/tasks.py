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

# 優先使用 main.reply_text；若不可用，以 lark_client 兜底
try:
    from .main import reply_text  # type: ignore
except Exception:
    reply_text = None  # type: ignore

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO))

DEFAULT_TZ = ZoneInfo("Asia/Taipei")


async def _send_reply(http: httpx.AsyncClient, chat_id: str, text: str) -> None:
    """統一的回覆函式：reply_text → lark_client.send_text_to_chat 兜底。"""
    try:
        if reply_text:
            await reply_text(http, chat_id, text)  # type: ignore
            return
    except Exception:
        pass
    try:
        from . import lark_client
        if hasattr(lark_client, "send_text_to_chat"):
            await lark_client.send_text_to_chat(http, chat_id, text)
            return
    except Exception:
        pass


def _yesterday_range(tz: str | ZoneInfo = DEFAULT_TZ) -> tuple[datetime, datetime]:
    """回傳當地時區的昨日 00:00 ~ 今日 00:00"""
    tz_obj = ZoneInfo(str(tz)) if isinstance(tz, str) else tz
    now = datetime.now(tz_obj)
    today = now.date()
    y = today - timedelta(days=1)
    start = datetime.combine(y, dt_time.min, tzinfo=tz_obj)
    end = datetime.combine(today, dt_time.min, tzinfo=tz_obj)
    return start, end


# ======================= 訊息記錄 =======================
async def record_message(event: Dict[str, Any]) -> None:
    """
    只負責落庫（文字型別），不觸發摘要指令，避免重覆回覆。
    """
    try:
        ev = event.get("event", {}) or {}
        msg = ev.get("message", {}) or {}
        if not msg:
            return
        chat_id = msg.get("chat_id")
        if not chat_id:
            return

        msg_type = (msg.get("message_type") or "").lower()
        raw = msg.get("content") or "{}"
        try:
            content = json.loads(raw) if isinstance(raw, str) else raw or {}
        except Exception:
            content = {}
        text = (content.get("text") or "").strip() if isinstance(content, dict) else ""

        # 取 sender_id
        sender_id = ""
        sender_info = msg.get("sender") or {}
        if isinstance(sender_info, dict):
            sid = sender_info.get("id")
            if isinstance(sid, dict):
                sender_id = sid.get("open_id") or sid.get("user_id") or ""
            else:
                sender_id = sender_info.get("sender_id") or sender_info.get("open_id") or (sid if isinstance(sid, str) else "")

        # 取毫秒時間戳
        try:
            ts_ms = int(msg.get("create_time") or 0)
        except Exception:
            ts_ms = int(datetime.utcnow().timestamp() * 1000)

        # 僅對文字訊息做保存
        if msg_type == "text" and text:
            async with AsyncSessionFactory() as db:
                await crud.upsert_chat(db, chat_id, None)  # 確保 chat 存在（不修改 enabled）
                await crud.save_message(db, chat_id=chat_id, text=text, sender_id=sender_id, ts_ms=ts_ms, msg_type="text")
    except Exception as e:
        logger.debug(f"record_message error: {e}")


# ======================= 指令處理 =======================
async def maybe_handle_summary_command(event: Dict[str, Any]) -> None:
    """
    支援：
      #summary once     -> 立即整理昨日
      #summary off      -> 關閉每日摘要
      #summary on       -> 開啟每日摘要
      #summary at HH    -> 設定每日摘要小時 0~23
      #summary tz Asia/Taipei -> 設定時區
      #summary lang zh|en     -> 設定語言
    """
    try:
        ev = event.get("event", {}) or {}
        msg = ev.get("message", {}) or {}
        chat_id = msg.get("chat_id")
        raw = msg.get("content", "{}")
        try:
            content = json.loads(raw) if isinstance(raw, str) else raw or {}
        except Exception:
            content = {}
        text = (content.get("text") or "").strip().lower() if isinstance(content, dict) else ""

        if not chat_id or not text.startswith("#summary"):
            return

        async with AsyncSessionFactory() as db:
            # 先確保 chat 存在
            await crud.upsert_chat(db, chat_id, None)

            # 1) once（先處理，避免被 on 吞掉）
            if text.startswith("#summary once"):
                async with httpx.AsyncClient() as http:
                    await summarize_for_single_chat(http, chat_id)
                return

            # 2) off
            if text.startswith("#summary off"):
                await crud.set_chat_enabled(db, chat_id, False)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, "已关闭本群每日摘要。")
                return

            # 3) on
            if text.startswith("#summary on"):
                await crud.set_chat_enabled(db, chat_id, True)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, "已开启本群每日摘要。")
                return

            # 4) at（設定小時）
            m = re.search(r"#summary\s+at\s+(\d{1,2})(?::\d{2})?", text)
            if m:
                hour = max(0, min(23, int(m.group(1))))
                await crud.set_chat_schedule(db, chat_id, hour=hour)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群每日摘要时间为 {hour:02d}:00。")
                return

            # 5) tz（設定時區）
            m = re.search(r"#summary\s+tz\s+([\w/\-]+)", text)
            if m:
                tz = m.group(1)
                await crud.set_chat_schedule(db, chat_id, tz=tz)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群摘要时区为 {tz}。")
                return

            # 6) lang（設定語言）
            m = re.search(r"#summary\s+lang\s+(zh|en)", text)
            if m:
                lang = m.group(1)
                await crud.set_chat_schedule(db, chat_id, lang=lang)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群摘要语言为 {lang}。")
                return

    except Exception as e:
        logger.debug(f"maybe_handle_summary_command error: {e}")


# ======================= 單群摘要（昨日） =======================
async def summarize_for_single_chat(
    http: httpx.AsyncClient, chat_id: str, tz: str | ZoneInfo = DEFAULT_TZ
) -> None:
    start, end = _yesterday_range(tz)
    async with AsyncSessionFactory() as db:
        msgs = await crud.get_messages_between(db, chat_id, start, end)

    if not msgs:
        await _send_reply(http, chat_id, f"（提示）{start.date()} 无聊天记录，略过摘要。")
        return

    # 聚合文字
    lines = [m.get("text", "") for m in msgs if isinstance(m, dict) and (m.get("text") or "").strip()]
    full_text = "\n".join(lines)
    try:
        summary = await summarize_text_or_fallback(http, full_text)
    except Exception:
        summary = "(降级) 摘要服务暂不可用。"

    await _send_reply(http, chat_id, f"【{start.date()} 日聊摘】\n{summary}")


# ======================= 全量摘要（定時） =======================
async def summarize_for_all_chats(http: httpx.AsyncClient) -> None:
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
    if not chats:
        return
    for c in chats:
        chat_id = c.get("chat_id")
        tz = c.get("tz") or "Asia/Taipei"
        if not chat_id:
            continue
        try:
            await summarize_for_single_chat(http, chat_id, tz=tz)
        except Exception:
            pass

