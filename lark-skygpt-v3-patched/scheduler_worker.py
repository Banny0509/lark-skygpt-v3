# app/scheduler_worker.py
import os
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import init_db, AsyncSessionFactory
from app import crud, tasks

logger = logging.getLogger("worker")
logging.basicConfig(level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO))

# 全局時區（兜底）
DEFAULT_TZ = getattr(settings, "TIMEZONE", "Asia/Taipei") or "Asia/Taipei"
SCAN_MINUTE = int(os.getenv("SCAN_MINUTE", "0"))  # 每小時第幾分掃描（預設 0：整點）

def _now_hour_in_tz(tz_name: str | None) -> int:
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    return datetime.now(tz).hour

def _today_str_in_tz(tz_name: str | None) -> str:
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    return str(datetime.now(tz).date())

async def _startup_readiness(max_wait_sec: int = 60) -> None:
    """確保 DB 建表完成再啟動排程，避免冷啟動時任務錯誤。"""
    deadline = asyncio.get_event_loop().time() + max_wait_sec
    while True:
        try:
            await init_db()
            logger.info("DB ready.")
            return
        except Exception as e:
            if asyncio.get_event_loop().time() > deadline:
                logger.warning("DB readiness timeout: %s", e)
                return
            await asyncio.sleep(2)

async def _enabled_chats() -> list[dict]:
    async with AsyncSessionFactory() as db:
        return await crud.get_all_chats(db)  # 只取 enabled=True

async def _summarize_chat_once_with_lock(http: httpx.AsyncClient, chat: dict, *, reason: str) -> None:
    """對單一群做摘要（昨日），使用 SummaryLock 防止同日重複。"""
    chat_id = chat.get("chat_id")
    tz = chat.get("tz") or DEFAULT_TZ
    if not chat_id:
        return
    today_str = _today_str_in_tz(tz)
    try:
        async with AsyncSessionFactory() as db:
            got = await crud.acquire_summary_lock(db, today_str, chat_id)
    except Exception as e:
        logger.warning("lock acquire failed chat=%s err=%s", chat_id, e)
        got = True  # 降級：不中斷
    if not got:
        logger.info("skip chat=%s: already summarized today (%s), reason=%s", chat_id, today_str, reason)
        return
    try:
        logger.info("summarizing chat=%s tz=%s reason=%s", chat_id, tz, reason)
        await tasks.summarize_for_single_chat(http, chat_id, tz=tz)
    except Exception as e:
        logger.exception("summarize failed chat=%s: %s", chat_id, e)

# --------------- 轨 1：每小時掃描，按群設定小時觸發 ---------------
async def _run_hourly_scan():
    chats = await _enabled_chats()
    if not chats:
        logger.info("__main__:hourly-scan: no active chats")
        return
    async with httpx.AsyncClient(timeout=30) as http:
        for c in chats:
            tz = c.get("tz") or DEFAULT_TZ
            hour = int(c.get("hour") or 8)
            if _now_hour_in_tz(tz) != hour:
                continue
            await _summarize_chat_once_with_lock(http, c, reason="hourly_scan")

# --------------- 轨 2：每天 08:00 兜底（以 DEFAULT_TZ） ---------------
async def _run_daily_fallback():
    """
    兜底：每天 DEFAULT_TZ 的 08:00 對所有啟用群再跑一輪。
    用與 hourly 相同的 Lock，避免重複。
    """
    chats = await _enabled_chats()
    if not chats:
        logger.info("__main__:daily-fallback: no active chats")
        return
    async with httpx.AsyncClient(timeout=30) as http:
        for c in chats:
            await _summarize_chat_once_with_lock(http, c, reason="daily_fallback")

async def main():
    logger.info("Worker starting… TZ=%s scan_minute=%s", DEFAULT_TZ, SCAN_MINUTE)
    await _startup_readiness()

    scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ)

    # 轨 1：每小時第 SCAN_MINUTE 分掃描（預設整點）
    scheduler.add_job(
        _run_hourly_scan,
        CronTrigger(minute=SCAN_MINUTE, second=0),
        id="hourly_chat_summary_scan",
        replace_existing=True,
    )

    # 轨 2：兜底：每天 08:00（DEFAULT_TZ）再跑一輪
    scheduler.add_job(
        _run_daily_fallback,
        CronTrigger(hour=8, minute=0, second=0),
        id="daily_fallback_all_chats",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started: hourly at *:%02d, and daily fallback at 08:00 (%s)", SCAN_MINUTE, DEFAULT_TZ)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())

