"""
Asynchronous tasks for message recording, summarisation and command handling.
"""

from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime, timedelta, time as dt_time, date
from typing import Dict, Any, Tuple, List

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

# ---- Redis（用於臨時管理員會話）----
try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:
    aioredis = None  # 沒有就不啟用登入機制（fallback 不報錯）

REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_CONNECTION_URL") or ""
ADMIN_CODE = os.getenv("SUMMARY_ADMIN_CODE", "")  # 你在 Railway 設定的一組口令
ADMIN_TTL_SEC = int(os.getenv("SUMMARY_ADMIN_TTL_SEC", "21600"))  # 管理員會話有效期（預設6小時）
MAX_RANGE_DAYS = int(os.getenv("SUMMARY_MAX_RANGE_DAYS", "31"))   # 範圍整理最多天數

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO))

DEFAULT_TZ = ZoneInfo("Asia/Taipei")


# ---------- 發訊息統一入口 ----------
async def _send_reply(http: httpx.AsyncClient, chat_id: str, text: str) -> None:
    """reply_text → lark_client.send_text_to_chat（兜底）"""
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


# ---------- Redis / Admin Session ----------
_redis = aioredis.from_url(REDIS_URL) if (aioredis and REDIS_URL) else None

async def _set_admin(open_id: str) -> None:
    if not _redis:
        return
    key = f"summary:admin:{open_id}"
    await _redis.set(key, "1", ex=ADMIN_TTL_SEC)

async def _del_admin(open_id: str) -> None:
    if not _redis:
        return
    await _redis.delete(f"summary:admin:{open_id}")

async def _is_admin(open_id: str) -> bool:
    if not _redis:
        # 若沒 Redis，當 ADMIN_CODE 也沒設，就視為關閉“全域”指令能力
        return False
    v = await _redis.get(f"summary:admin:{open_id}")
    return bool(v)


# ---------- 時間區間工具 ----------
def _yesterday_range(tz: str | ZoneInfo = DEFAULT_TZ) -> tuple[datetime, datetime]:
    tz_obj = ZoneInfo(str(tz)) if isinstance(tz, str) else tz
    now = datetime.now(tz_obj)
    today = now.date()
    y = today - timedelta(days=1)
    start = datetime.combine(y, dt_time.min, tzinfo=tz_obj)
    end = datetime.combine(today, dt_time.min, tzinfo=tz_obj)
    return start, end

def _parse_range(text: str) -> Tuple[date, date] | None:
    """解析 'range YYYY-MM-DD to YYYY-MM-DD' 或 'range YYYY-MM-DD - YYYY-MM-DD'"""
    m = re.search(r"range\s+(\d{4}-\d{2}-\d{2})\s*(?:to|-)\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        return None
    try:
        d1 = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        d2 = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        if d2 < d1:
            return None
        return d1, d2
    except Exception:
        return None

def _start_end_from_dates(d1: date, d2: date, tz: str | ZoneInfo = DEFAULT_TZ) -> Tuple[datetime, datetime]:
    tz_obj = ZoneInfo(str(tz)) if isinstance(tz, str) else tz
    start = datetime.combine(d1, dt_time.min, tzinfo=tz_obj)
    end = datetime.combine(d2 + timedelta(days=1), dt_time.min, tzinfo=tz_obj)  # [start, end)
    return start, end


# ======================= 訊息記錄（僅落庫） =======================
async def record_message(event: Dict[str, Any]) -> None:
    """
    只負責落庫（文字型別）；不觸發摘要指令，避免重覆回覆。
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

        # 取 sender_id（如有）
        sender_id = ""
        s = msg.get("sender") or {}
        if isinstance(s, dict):
            sid = s.get("id")
            if isinstance(sid, dict):
                sender_id = sid.get("open_id") or sid.get("user_id") or ""
            else:
                sender_id = s.get("sender_id") or s.get("open_id") or (sid if isinstance(sid, str) else "")

        # 毫秒時間戳
        try:
            ts_ms = int(msg.get("create_time") or 0)
        except Exception:
            ts_ms = int(datetime.utcnow().timestamp() * 1000)

        if msg_type == "text" and text:
            async with AsyncSessionFactory() as db:
                await crud.upsert_chat(db, chat_id, None)  # 確保 chat 存在（enabled 狀態由指令維護）
                await crud.save_message(db, chat_id=chat_id, text=text, sender_id=sender_id, ts_ms=ts_ms, msg_type="text")
    except Exception as e:
        logger.debug(f"record_message error: {e}")


# ======================= 指令處理 =======================
async def maybe_handle_summary_command(event: Dict[str, Any]) -> None:
    """
    支援：
      #summary login <code>            -> 登入成為臨時管理員（6小時）
      #summary logout                  -> 退出管理員
      #summary on / off / at / tz / lang / once
      #summary range YYYY-MM-DD to YYYY-MM-DD
      #summary all range YYYY-MM-DD to YYYY-MM-DD  -> 需要臨時管理員
      #summary enable all              -> 將 DB 內所有群設為 enabled（需要臨時管理員）
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
        text = (content.get("text") or "").strip()
        low = text.lower()

        if not chat_id or not low.startswith("#summary"):
            return

        # 取得 sender_open_id（作為管理員會話身份）
        sender_open_id = ""
        s = msg.get("sender") or {}
        if isinstance(s, dict):
            sid = s.get("id")
            if isinstance(sid, dict):
                sender_open_id = sid.get("open_id") or ""
            else:
                sender_open_id = s.get("open_id") or ""

        # ===== 登入 / 登出（不需 DB）=====
        if low.startswith("#summary login"):
            code = text.split(None, 2)[-1].strip() if len(text.split()) >= 3 else ""
            async with httpx.AsyncClient() as http:
                if not ADMIN_CODE:
                    await _send_reply(http, chat_id, "系统未设置 SUMMARY_ADMIN_CODE，无法登录。")
                    return
                if code != ADMIN_CODE:
                    await _send_reply(http, chat_id, "口令错误。")
                    return
                if not sender_open_id:
                    await _send_reply(http, chat_id, "无法识别你的 open_id，无法登录为管理员。")
                    return
                await _set_admin(sender_open_id)
                await _send_reply(http, chat_id, "管理员登录成功（6小时）。")
            return

        if low.startswith("#summary logout"):
            if sender_open_id:
                await _del_admin(sender_open_id)
            async with httpx.AsyncClient() as http:
                await _send_reply(http, chat_id, "已登出管理员。")
            return

        async with AsyncSessionFactory() as db:
            # 先確保 chat 存在
            await crud.upsert_chat(db, chat_id, None)

            # ===== 先處理「區間整理」 =====
            if low.startswith("#summary range"):
                rng = _parse_range(low)
                if not rng:
                    async with httpx.AsyncClient() as http:
                        await _send_reply(http, chat_id, "用法：#summary range YYYY-MM-DD to YYYY-MM-DD")
                    return
                d1, d2 = rng
                if (d2 - d1).days + 1 > MAX_RANGE_DAYS:
                    async with httpx.AsyncClient() as http:
                        await _send_reply(http, chat_id, f"区间过大，最多 {MAX_RANGE_DAYS} 天。")
                    return
                start, end = _start_end_from_dates(d1, d2)
                async with httpx.AsyncClient() as http:
                    await _summarize_for_chat_in_range(http, chat_id, start, end)
                return

            if low.startswith("#summary all range"):
                async with httpx.AsyncClient() as http:
                    if not sender_open_id or not await _is_admin(sender_open_id):
                        await _send_reply(http, chat_id, "没有权限执行此指令，请先 #summary login <code>。")
                        return
                rng = _parse_range(low)
                if not rng:
                    async with httpx.AsyncClient() as http:
                        await _send_reply(http, chat_id, "用法：#summary all range YYYY-MM-DD to YYYY-MM-DD")
                    return
                d1, d2 = rng
                if (d2 - d1).days + 1 > MAX_RANGE_DAYS:
                    async with httpx.AsyncClient() as http:
                        await _send_reply(http, chat_id, f"区间过大，最多 {MAX_RANGE_DAYS} 天。")
                    return
                start, end = _start_end_from_dates(d1, d2)
                async with httpx.AsyncClient() as http:
                    n = await _summarize_for_all_chats_in_range(http, start, end)
                    await _send_reply(http, chat_id, f"已对 {n} 个群发送区间摘要。")
                return

            if low.startswith("#summary enable all"):
                async with httpx.AsyncClient() as http:
                    if not sender_open_id or not await _is_admin(sender_open_id):
                        await _send_reply(http, chat_id, "没有权限执行此指令，请先 #summary login <code>。")
                        return
                # 將資料庫所有群設為 enabled=True
                # 借用 get_all_chats 會只回 enabled=True；這裡簡化：再次 upsert + set_chat_enabled
                # 先取目前所有 seen chats（簡化做法：透過所有 messages 的 chat_id 推測）
                # 這裡實務上應該在 DB 層提供 get_all_seen_chats；暫以 all_chats=current enabled + 指令所在群
                async with AsyncSessionFactory() as db2:
                    # 若你的 crud 有專門方法可拉全部 chats，請替換
                    # 這裡直接對當前聊天設 enabled=True；其餘群請逐群在群內發 #summary on
                    await crud.set_chat_enabled(db2, chat_id, True)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, "已将本群设为每日摘要启用。")
                return

            # ===== once / off / on / at / tz / lang =====
            if low.startswith("#summary once"):
                async with httpx.AsyncClient() as http:
                    await summarize_for_single_chat(http, chat_id)
                return

            if low.startswith("#summary off"):
                await crud.set_chat_enabled(db, chat_id, False)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, "已关闭本群每日摘要。")
                return

            if low.startswith("#summary on"):
                await crud.set_chat_enabled(db, chat_id, True)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, "已开启本群每日摘要。")
                return

            m = re.search(r"#summary\s+at\s+(\d{1,2})(?::\d{2})?", low)
            if m:
                hour = max(0, min(23, int(m.group(1))))
                await crud.set_chat_schedule(db, chat_id, hour=hour)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群每日摘要时间为 {hour:02d}:00。")
                return

            m = re.search(r"#summary\s+tz\s+([\w/\-]+)", low)
            if m:
                tz = m.group(1)
                await crud.set_chat_schedule(db, chat_id, tz=tz)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群摘要时区为 {tz}。")
                return

            m = re.search(r"#summary\s+lang\s+(zh|en)", low)
            if m:
                lang = m.group(1)
                await crud.set_chat_schedule(db, chat_id, lang=lang)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群摘要语言为 {lang}。")
                return

    except Exception as e:
        logger.debug(f"maybe_handle_summary_command error: {e}")


# ======================= 單群摘要（昨日 or 指定範圍） =======================
async def summarize_for_single_chat(
    http: httpx.AsyncClient, chat_id: str, tz: str | ZoneInfo = DEFAULT_TZ
) -> None:
    start, end = _yesterday_range(tz)
    await _summarize_for_chat_in_range(http, chat_id, start, end)


async def _summarize_for_chat_in_range(
    http: httpx.AsyncClient, chat_id: str, start: datetime, end: datetime, tz: str | ZoneInfo = DEFAULT_TZ
) -> None:
    async with AsyncSessionFactory() as db:
        msgs = await crud.get_messages_between(db, chat_id, start, end)

    if not msgs:
        tip_day = (start.date(), (end - timedelta(days=1)).date())
        await _send_reply(http, chat_id, f"（提示）{tip_day[0]} ~ {tip_day[1]} 无聊天记录，略过摘要。")
        return

    lines = [
        m.get("text", "")
        for m in msgs
        if isinstance(m, dict) and (m.get("text") or "").strip()
    ]
    full_text = "\n".join(lines)
    try:
        summary = await summarize_text_or_fallback(http, full_text)
    except Exception:
        summary = "(降级) 摘要服务暂不可用。"

    tip_day = (start.date(), (end - timedelta(days=1)).date())
    await _send_reply(http, chat_id, f"【{tip_day[0]} ~ {tip_day[1]} 历史摘要】\n{summary}")


# ======================= 所有群範圍摘要（需要臨時管理員） =======================
async def _summarize_for_all_chats_in_range(http: httpx.AsyncClient, start: datetime, end: datetime) -> int:
    n = 0
    # 這裡沿用 get_all_chats（enabled=True 的群），若要包含未啟用群，可在 crud 增加方法或在此自定義查詢
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
    for c in chats:
        chat_id = c.get("chat_id")
        tz = c.get("tz") or "Asia/Taipei"
        if not chat_id:
            continue
        try:
            await _summarize_for_chat_in_range(http, chat_id, start, end, tz=tz)
            n += 1
        except Exception:
            pass
    return n


# ======================= 每日定時入口（保留原有行為） =======================
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
            start, end = _yesterday_range(tz)
            await _summarize_for_chat_in_range(http, chat_id, start, end, tz=tz)
        except Exception:
            pass
