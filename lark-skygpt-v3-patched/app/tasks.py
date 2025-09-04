"""
Asynchronous tasks for message recording, summarisation and command handling.
"""

from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime, timedelta, time as dt_time, date
from typing import Dict, Any, Tuple

import httpx
from zoneinfo import ZoneInfo

from .openai_client import summarize_text_or_fallback
from .database import AsyncSessionFactory
from . import crud

# 优先使用 main.reply_text（Web 端可用）
try:
    from .main import reply_text  # type: ignore
except Exception:
    reply_text = None  # type: ignore

# Redis（用于管理员临时登录，会话可绑定“用户 open_id”或“当前群”）
try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:
    aioredis = None

# 环境变量
REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_CONNECTION_URL") or ""
ADMIN_CODE = os.getenv("SUMMARY_ADMIN_CODE", "")
ADMIN_TTL_SEC = int(os.getenv("SUMMARY_ADMIN_TTL_SEC", "21600"))  # 默认 6 小时
MAX_RANGE_DAYS = int(os.getenv("SUMMARY_MAX_RANGE_DAYS", "31"))

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO))

DEFAULT_TZ = ZoneInfo("Asia/Taipei")
_redis = aioredis.from_url(REDIS_URL) if (aioredis and REDIS_URL) else None


# ========== 统一回覆 ==========
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


# ========== 管理员会话（用户/群） ==========
async def _set_admin(open_id: str) -> None:
    if not _redis or not open_id:
        return
    await _redis.set(f"summary:admin:{open_id}", "1", ex=ADMIN_TTL_SEC)

async def _del_admin(open_id: str) -> None:
    if not _redis or not open_id:
        return
    await _redis.delete(f"summary:admin:{open_id}")

async def _set_admin_chat(chat_id: str) -> None:
    if not _redis or not chat_id:
        return
    await _redis.set(f"summary:admin_chat:{chat_id}", "1", ex=ADMIN_TTL_SEC)

async def _del_admin_chat(chat_id: str) -> None:
    if not _redis or not chat_id:
        return
    await _redis.delete(f"summary:admin_chat:{chat_id}")

async def _is_admin_both(open_id: str, chat_id: str) -> bool:
    """既检查用户会话，也检查本群会话"""
    if not _redis:
        return False
    if open_id and await _redis.get(f"summary:admin:{open_id}"):
        return True
    if chat_id and await _redis.get(f"summary:admin_chat:{chat_id}"):
        return True
    return False


# ========== 时间工具 ==========
def _yesterday_range(tz: str | ZoneInfo = DEFAULT_TZ) -> Tuple[datetime, datetime]:
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
    except Exception:
        return None
    if d2 < d1:
        return None
    return d1, d2

def _start_end_from_dates(d1: date, d2: date, tz: str | ZoneInfo = DEFAULT_TZ) -> Tuple[datetime, datetime]:
    tz_obj = ZoneInfo(str(tz)) if isinstance(tz, str) else tz
    start = datetime.combine(d1, dt_time.min, tzinfo=tz_obj)
    end = datetime.combine(d2 + timedelta(days=1), dt_time.min, tzinfo=tz_obj)  # [start, end)
    return start, end


# ========== 落库：仅保存文字 ==========
async def record_message(event: Dict[str, Any]) -> None:
    """只把文字消息写入 DB，不触发指令，避免重复回复。"""
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

        # sender_id（可选）
        sender_id = ""
        s = msg.get("sender") or {}
        if isinstance(s, dict):
            sid = s.get("id")
            if isinstance(sid, dict):
                sender_id = sid.get("open_id") or sid.get("user_id") or ""
            else:
                sender_id = s.get("sender_id") or s.get("open_id") or (sid if isinstance(sid, str) else "")

        # 毫秒时间戳
        try:
            ts_ms = int(msg.get("create_time") or 0)
        except Exception:
            ts_ms = int(datetime.utcnow().timestamp() * 1000)

        if msg_type == "text" and text:
            async with AsyncSessionFactory() as db:
                await crud.upsert_chat(db, chat_id, None)
                await crud.save_message(db, chat_id=chat_id, text=text, sender_id=sender_id, ts_ms=ts_ms, msg_type="text")
    except Exception as e:
        logger.debug(f"record_message error: {e}")


# ========== 指令处理 ==========
async def maybe_handle_summary_command(event: Dict[str, Any]) -> None:
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

        # 取得 open_id（优先 event.sender.sender_id.open_id，再回退 message.sender）
        sender_open_id = ""
        sender_ev = ev.get("sender") or {}
        sid_ev = sender_ev.get("sender_id") if isinstance(sender_ev, dict) else None
        if isinstance(sid_ev, dict):
            sender_open_id = sid_ev.get("open_id") or sid_ev.get("user_id") or sid_ev.get("union_id") or ""
        else:
            sender_open_id = sender_ev.get("open_id") if isinstance(sender_ev, dict) else ""
        if not sender_open_id:
            sender_msg = msg.get("sender") or {}
            sid_msg = sender_msg.get("sender_id") or sender_msg.get("id")
            if isinstance(sid_msg, dict):
                sender_open_id = sid_msg.get("open_id") or sender_open_id

        # 登录 / 登出
        if low.startswith("#summary login"):
            code = text.split(None, 2)[-1].strip() if len(text.split()) >= 3 else ""
            async with httpx.AsyncClient() as http:
                if not ADMIN_CODE:
                    await _send_reply(http, chat_id, "系统未设置 SUMMARY_ADMIN_CODE，无法登录。")
                    return
                if code != ADMIN_CODE:
                    await _send_reply(http, chat_id, "口令错误。")
                    return
                if sender_open_id:
                    await _set_admin(sender_open_id)
                    await _send_reply(http, chat_id, "管理员登录成功（按用户，6小时）。")
                else:
                    await _set_admin_chat(chat_id)
                    await _send_reply(http, chat_id, "管理员登录成功（按本群，6小时）。")
            return

        if low.startswith("#summary logout"):
            if sender_open_id:
                await _del_admin(sender_open_id)
            await _del_admin_chat(chat_id)
            async with httpx.AsyncClient() as http:
                await _send_reply(http, chat_id, "已登出管理员。")
            return

        async with AsyncSessionFactory() as db:
            await crud.upsert_chat(db, chat_id, None)

            # 本群区间摘要
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

            # 所有启用群区间摘要（需登录）
            if low.startswith("#summary all range"):
                async with httpx.AsyncClient() as http:
                    if not await _is_admin_both(sender_open_id, chat_id):
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

            # 立即整理（昨日）
            if low.startswith("#summary once"):
                async with httpx.AsyncClient() as http:
                    await summarize_for_single_chat(http, chat_id)
                return

            # 开启 / 关闭
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

            # 修改时间 / 时区 / 语言
            m = re.search(r"#summary\s+at\s+(\d{1,2})(?::\d{2})?", low)
            if m:
                hour = max(0, min(23, int(m.group(1))))
                await crud.set_chat_schedule(db, chat_id, hour=hour)
                async with httpx.AsyncClient() as http:
                    await _send_reply(http, chat_id, f"已更新本群每日摘要时间为 {hour:02d}:00。")
                return

            m = re.search(r"#summary\s+tz\s+([\w/\\-]+)", low)
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


# ========== 摘要产出 ==========
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

    tip_start = start.date()
    tip_end = (end - timedelta(days=1)).date()

    if not msgs:
        await _send_reply(http, chat_id, f"（提示）{tip_start} ~ {tip_end} 无聊天记录，略过摘要。")
        return

    # 整理文本
    lines = [
        m.get("text", "")
        for m in msgs
        if isinstance(m, dict) and (m.get("text") or "").strip()
    ]
    full_text = "\n".join(lines)

    # 取得原始摘要
    try:
        raw_summary = await summarize_text_or_fallback(http, full_text)
    except Exception:
        raw_summary = "(降级) 摘要服务暂不可用。"

    # 统一格式：拆分行 → 分类（重點提醒/主要討論）
    topics: list[str] = []
    reminders: list[str] = []
    for line in raw_summary.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(k in line for k in ["期限", "截止", "交期", "提醒", "待办", "确认", "出问题", "色差", "瑕疵", "来不及", "TODO"]):
            reminders.append(line)
        else:
            topics.append(line)

    if not topics:
        topics.append("无主要讨论内容")
    if not reminders:
        reminders.append("无")

    # 产出统一格式
    header = f"【{tip_start} ~ {tip_end} 日讯息摘要】\n\n"
    main_section = "・主要讨论事项：\n" + "\n".join(f"  {i+1}. {t}" for i, t in enumerate(topics))
    reminder_section = "・重点提醒：\n" + "\n".join(f"  − {r}" for r in reminders)
    formatted = header + main_section + "\n\n" + reminder_section

    await _send_reply(http, chat_id, formatted)


# ========== 全群区间摘要 ==========
async def _summarize_for_all_chats_in_range(http: httpx.AsyncClient, start: datetime, end: datetime) -> int:
    count = 0
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
    for c in chats:
        chat_id = c.get("chat_id")
        tz = c.get("tz") or "Asia/Taipei"
        if not chat_id:
            continue
        try:
            await _summarize_for_chat_in_range(http, chat_id, start, end, tz=tz)
            count += 1
        except Exception:
            pass
    return count


# ========== 每日入口（保留原行为） ==========
async def summarize_for_all_chats(http: httpx.AsyncClient) -> None:
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
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

            

