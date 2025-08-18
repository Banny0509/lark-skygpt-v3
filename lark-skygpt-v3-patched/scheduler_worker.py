# scheduler_worker.py
import os
import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from app.config import settings
from app.database import init_db
from app import tasks, lark_client, utils

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s worker :: %(message)s",
)
logger = logging.getLogger("worker")

RETRY_DELAY_SEC = 5


async def _startup_readiness():
    """
    在冷啟動時做基本就緒檢查，避免一開始因 DB/Redis/Lark token 還沒就緒就整個退出。
    """
    # DB init（內含 Engine 準備）
    for i in range(10):
        try:
            await init_db()
            logger.info("DB init ok")
            break
        except Exception as e:
            logger.warning("DB init failed (try %s): %s", i + 1, e)
            await asyncio.sleep(RETRY_DELAY_SEC)
    else:
        logger.error("DB init failed after retries, continue anyway (job may fail later).")

    # Lark token（可選：確認 AppID/Secret 是否有效）
    async with httpx.AsyncClient(timeout=15) as http:
        for i in range(10):
            try:
                # 取 tenant_access_token（會用到 APP_ID/APP_SECRET/LARK_BASE）
                _ = await lark_client.get_tenant_access_token(http)
                logger.info("Lark token ok")
                break
            except Exception as e:
                logger.warning("Lark token failed (try %s): %s", i + 1, e)
                await asyncio.sleep(RETRY_DELAY_SEC)
        else:
            logger.error("Lark token failed after retries; summary job may fail.")


async def _run_daily_summary():
    """
    實際執行每日摘要（所有群組）。
    """
    logger.info("Daily summary job start")
    async with httpx.AsyncClient(timeout=60) as http:
        try:
            await tasks.summarize_for_all_chats(http)
            logger.info("Daily summary job done")
        except Exception as e:
            logger.exception("Daily summary job failed: %s", e)


async def _smoke_test_once():
    """
    啟動後做一次極簡自檢（不發訊息，只印 log），幫你在 Logs 上看到 worker 活著。
    """
    now = utils.now_local()
    logger.info("Worker smoke test at %s (%s), OPENAI=%s",
                now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                settings.TIMEZONE,
                bool(settings.OPENAI_API_KEY))


def _on_job_event(event):
    if event.exception:
        logger.error("Job crashed: %s", event)
    else:
        logger.info("Job finished: %s", event)


async def main():
    logger.info("Worker starting... TZ=%s", settings.TIMEZONE)
    await _startup_readiness()

    scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)
    scheduler.add_listener(_on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    # 每天 08:00 觸發（你可依需求調整）
    trigger = CronTrigger(hour=8, minute=0, second=0, timezone=settings.TIMEZONE)
    scheduler.add_job(_run_daily_summary, trigger, id="daily_summary", replace_existing=True)

    scheduler.start()
    logger.info("Scheduler started with TZ=%s", settings.TIMEZONE)

    # 列出下一次觸發時間，方便你在 Logs 看到
    next_run = scheduler.get_job("daily_summary").next_run_time
    logger.info("Next daily_summary run at: %s", next_run)

    # 啟動後做一次輕量 smoke test
    await _smoke_test_once()

    # 保持運行
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Worker shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
