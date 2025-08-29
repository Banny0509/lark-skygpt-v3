# scheduler_worker.py
import os
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import init_db, AsyncSessionFactory
from app import crud, tasks

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, (os.getenv("LOG_LEVEL") or "INFO").upper(), logging.INFO))

DEFAULT_TZ = getattr(settings, "TIMEZONE", "Asia/Taipei") or "Asia/Taipei"
SCAN_MINUTE = int(os.getenv("SCAN_MINUTE", "0"))

async def _startup_readiness(max_wait_sec: int = 60) -> None:
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

def _now_hour_in_tz(tz_name: str | None) -> int:
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    return datetime.now(tz).hour

def _today_str_in_tz(tz_name: str | None) -> str:
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    return str(datetime.now(tz).date())

async def _run_hourly_scan():
    async with AsyncSessionFactory() as db:
        chats = await crud.get_all_chats(db)
    if not chats:
        logger.info("hourly-scan: no active chats")
        return

    async with httpx.AsyncClient(timeout=20) as http:
        for c in chats:
            chat_id = c.get("chat_id")
            tz = c.get("tz") or DEFAULT_TZ
            hour = int(c.get("hour") or 8)
            if _now_hour_in_tz(tz) != hour:
                continue

            today_str = _today_str_in_tz(tz)
            try:
                async with AsyncSessionFactory() as db:
                    got = await crud.acquire_summary_lock(db, today_str, chat_id)
            except Exception as e:
                logger.warning("lock acquire failed chat=%s err=%s", chat_id, e)
                got = True
            if not got:
                logger.info("skip chat=%s: already summarized today (%s)", chat_id, today_str)
                continue

            try:
                logger.info("summarizing chat=%s tz=%s hour=%s", chat_id, tz, hour)
                await tasks.summarize_for_single_chat(http, chat_id, tz=tz)
            except Exception as e:
                logger.exception("summarize failed chat=%s: %s", chat_id, e)

async def main():
    logger.info("Worker starting… TZ=%s scan_minute=%s", DEFAULT_TZ, SCAN_MINUTE)
    await _startup_readiness()
    scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ)
    scheduler.add_job(_run_hourly_scan, CronTrigger(minute=SCAN_MINUTE, second=0),
                      id="hourly_chat_summary_scan", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started: hourly at *:%02d", SCAN_MINUTE)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())
