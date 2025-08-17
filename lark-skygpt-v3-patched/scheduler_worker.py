import asyncio
import logging
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.tasks import run_daily_summary_per_chat

logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger("scheduler")

async def main():
    logger.info("Scheduler worker starting...")
    tz = ZoneInfo(settings.TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        run_daily_summary_per_chat,
        CronTrigger(hour=8, minute=0, second=0, timezone=tz),
        id="daily_summary_0800_per_chat",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("Scheduler started (08:00 %s)", settings.TIMEZONE)
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler worker stopped.")
