import os
import json
import logging
import httpx
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from . import openai_client

# 有就用，沒有就降級（不中斷）
try:
    from . import crud  # noqa
    _HAS_CRUD = True
except Exception:
    crud = None  # type: ignore
    _HAS_CRUD = False

try:
    from . import lark_client  # noqa
    _HAS_LARK = True
except Exception:
    lark_client = None  # type: ignore
    _HAS_LARK = False

logger = logging.getLogger(__name__)
TZ = ZoneInfo(os.getenv("TZ", "Asia/Taipei"))

def _yesterday_range() -> tuple[datetime, datetime]:
    now = datetime.now(TZ)
    today = now.date()
    y = today - timedelta(days=1)
    start = datetime.combine(y, dt_time.min, tzinfo=TZ)       # 昨日 00:00（含）
    end   = datetime.combine(today, dt_time.min, tzinfo=TZ)    # 今日 00:00（不含）
    return start, end

def _summary_chat_ids() -> list[str]:
    """若 DB 無 get_all_chats，則用環境變數 SUMMARY_CHAT_IDS=oc_xxx,oc_yyy"""
    ids_env = os.getenv("SUMMARY_CHAT_IDS", "")
    return [i.strip() for i in ids_env.split(",") if i.strip()]

async def _send_text(http: httpx.AsyncClient, chat_id: str, text: str):
    if _HAS_LARK and hasattr(lark_client, "send_text_to_chat"):
        await lark_client.send_text_to_chat(http, chat_id, text)
        return
    if hasattr(openai_client, "reply_text"):
        await openai_client.reply_text(http, chat_id, text, by_chat_id=True)  # type: ignore

async def summarize_for_single_chat(http: httpx.AsyncClient, chat_id: str):
    start, end = _yesterday_range()

    msgs = []
    if _HAS_CRUD and hasattr(crud, "get_messages_between"):
        try:
            msgs = await crud.get_messages_between(chat_id, start, end)  # type: ignore
        except Exception as e:
            logger.error("讀取訊息失敗：%s", e)

    if not msgs:
        await _send_text(http, chat_id, f"昨日（{start.date()}）沒有聊天記錄。")
        return

    text = "\n".join([f"{m['text']}" for m in msgs if isinstance(m, dict) and m.get("text")])
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
""".strip()
    summary = await openai_client.summarize_text_or_fallback(http, prompt)
    await _send_text(http, chat_id, f"【昨日聊天摘要】\n{summary}")

async def summarize_for_all_chats(http: httpx.AsyncClient):
    chat_ids: list[str] = []
    if _HAS_CRUD and hasattr(crud, "get_all_chats"):
        try:
            chats = await crud.get_all_chats()  # type: ignore
            chat_ids = [c["chat_id"] for c in chats if isinstance(c, dict) and c.get("chat_id")]
        except Exception as e:
            logger.error("讀取群組列表失敗：%s", e)
    if not chat_ids:
        chat_ids = _summary_chat_ids()

    for cid in chat_ids:
        try:
            await summarize_for_single_chat(http, cid)
        except Exception as e:
            logger.error("摘要失敗 chat=%s err=%s", cid, e)

async def record_message(event: dict):
    """寫 DB：若無 crud.save_message 則安靜略過。"""
    if not (_HAS_CRUD and hasattr(crud, "save_message")):
        return
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
        ts = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=TZ) if ts_ms else datetime.now(TZ)
        if chat_id and text:
            await crud.save_message(chat_id, sender, text, ts)  # type: ignore
    except Exception as e:
        logger.error("記錄訊息失敗: %s", e)

# ====== Scheduler：每天 08:30 (Asia/Taipei) 發昨日摘要 ======
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

async def _run_daily_summary():
    logger.info("Daily summary job start")
    async with httpx.AsyncClient() as http:
        await summarize_for_all_chats(http)

def setup_scheduler():
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(_run_daily_summary, CronTrigger(hour=8, minute=30, second=0))
    scheduler.start()
    logger.info("Scheduler started (08:30 Asia/Taipei)")
