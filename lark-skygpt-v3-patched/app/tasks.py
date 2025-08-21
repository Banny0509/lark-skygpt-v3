# app/tasks.py

import logging
import json
import httpx
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from . import crud, utils, openai_client, lark_client

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Taipei")

def _yesterday_range() -> tuple[datetime, datetime]:
    """
    取得『昨天 00:00』到『今天 00:00』（end 為「不含」），避免跨天重疊。
    """
    now = utils.now_local() if hasattr(utils, "now_local") else datetime.now(TZ)
    today = now.astimezone(TZ).date()
    y = today - timedelta(days=1)
    start = datetime.combine(y, dt_time.min, tzinfo=TZ)
    end = datetime.combine(today, dt_time.min, tzinfo=TZ)  # 今天 00:00 (exclusive)
    return start, end

async def summarize_for_single_chat(http: httpx.AsyncClient, chat_id: str):
    """
    從資料庫拉取『昨天 00:00–24:00』聊天記錄，交給 OpenAI 產生摘要，並送回群組。
    """
    start, end = _yesterday_range()
    msgs = await crud.get_messages_between(chat_id, start, end)

    if not msgs:
        await lark_client.send_text_to_chat(http, chat_id, f"昨日（{start.date()}）沒有聊天記錄。")
        return

    # 把純文字訊息串接（保留你原有格式）
    text = "\n".join([f"{m['text']}" for m in msgs if m.get("text")])

    prompt = f"""
你是一個會議與聊天摘要專家，請幫我整理昨日聊天內容（時間範圍：{start:%Y-%m-%d 00:00} ~ {end:%Y-%m-%d 00:00}），輸出格式必須如下（保持中文，條列式）：

昨日聊天摘要 ({start.date()}):
 群組聊天記錄摘要

1. 關鍵決策：
   - 請列出昨天討論中做出的決策或結論
2. 待辦事項：
   - 請整理昨天分派的任務或工作（包含負責人與期限若有提到）
3. 未決問題：
   - 請列出還沒有結論、需要後續討論的議題
4. 其他資訊：
   - 其他有用的資訊或重點

以下是聊天記錄：
{text}
"""
    summary = await openai_client.summarize_text_or_fallback(http, prompt)
    await lark_client.send_text_to_chat(http, chat_id, f"【昨日聊天摘要】\n{summary}")

async def summarize_for_all_chats(http: httpx.AsyncClient):
    """
    對所有群組執行昨日摘要（保留你原本 API）。
    """
    chats = await crud.get_all_chats()
    for chat in chats:
        try:
            await summarize_for_single_chat(http, chat["chat_id"])
        except Exception as e:
            logger.error(f"摘要失敗 chat={chat.get('chat_id')} error={e}")

async def record_message(event: dict):
    """
    儲存聊天訊息到資料庫（健壯解析，避免 KeyError）。
    僅存純文字內容，其他型別你可在 crud 端擴充欄位。
    """
    try:
        ev = event.get("event", {}) or {}
        msg = ev.get("message", {}) or {}
        chat_id = msg.get("chat_id")
        content_raw = msg.get("content", "{}")
        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
        except Exception:
            content = {}
        text = (content.get("text") or "").strip()
        sender = (ev.get("sender", {}) or {}).get("sender_id", {}).get("open_id") or ""
        ts_ms = msg.get("create_time")
        if ts_ms is None:
            # 退而求其次：utils.now_local
            ts = utils.now_local() if hasattr(utils, "now_local") else datetime.now(TZ)
        else:
            ts = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=TZ)

        if chat_id and text:
            await crud.save_message(chat_id, sender, text, ts)
    except Exception as e:
        logger.error(f"記錄訊息失敗: {e}")

# =========================
# （供 Worker 使用）每天 08:30 發昨日摘要
# =========================
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

async def _run_daily_summary():
    async with httpx.AsyncClient() as http:
        await summarize_for_all_chats(http)

def setup_scheduler():
    """
    由 scheduler_worker 或啟動程序呼叫。
    Asia/Taipei 每天 08:30 觸發 _run_daily_summary。
    """
    scheduler = AsyncIOScheduler(timezone=TZ)
    # 08:30 觸發（台北）
    scheduler.add_job(_run_daily_summary, CronTrigger(hour=8, minute=30))
    scheduler.start()
    logger.info("Scheduler started with TZ=Asia/Taipei; next run 08:30 daily")
