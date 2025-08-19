
# scheduler_worker.py
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import httpx

from app.config import settings
from app.openai_client import summarize
from app.lark_client import reply_text

logger = logging.getLogger("scheduler")
logging.basicConfig(level=logging.INFO)

async def do_daily_summary(http: httpx.AsyncClient):
    # 预留：可在 Redis 维护 chat_id 订阅清单，这里仅占位
    pass

async def main():
    scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
    scheduler.add_job(do_daily_summary, CronTrigger(hour=8, minute=0), args=[httpx.AsyncClient()])
    scheduler.start()
    logger.info("Scheduler started (Asia/Taipei 08:00 daily).")
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(main())
