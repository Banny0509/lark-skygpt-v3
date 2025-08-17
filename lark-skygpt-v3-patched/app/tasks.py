import re
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from .database import AsyncSessionFactory
from . import utils, lark_client, crud, openai_client
import httpx

SUMMARY_PROMPT = """你是資深會議/群組助理。請將下列聊天訊息整理成摘要（繁體中文）：
輸出格式：
(YYYY-MM-DD):
### 群組聊天記錄摘要

#### 關鍵決策
- ...

#### 待辦事項
- ...

#### 未決問題
- ...
僅總結「昨天（本地時區）」00:00–24:00 的內容，簡潔具體。"""

def _yesterday_title() -> str:
    y = utils.now_local().date() - __import__('datetime').timedelta(days=1)
    return f"({y.isoformat()}):"

async def summarize_for_single_chat(http: httpx.AsyncClient, chat_id: str):
    start_dt, end_dt = utils.yesterday_range_local()
    start_ms, end_ms = utils.to_epoch_ms(start_dt), utils.to_epoch_ms(end_dt)
    date_tag = start_dt.date().isoformat()

    async with AsyncSessionFactory() as db:
        if not await crud.acquire_summary_lock(db, date_tag, chat_id):
            return

    try:
        items = await lark_client.list_chat_messages_between(http, chat_id, start_ms, end_ms)
    except Exception:
        items = []

    snippets: List[str] = []
    for it in items:
        msg = it.get("message") or it
        msg_type = msg.get("message_type")
        if msg_type == "text":
            try:
                content = __import__('json').loads(msg.get("content") or "{}")
            except Exception:
                content = {}
            t = (content.get("text") or "").strip()
            if t:
                clean = re.sub(r"<.*?>", "", t)[:300]
                if clean:
                    snippets.append(clean)

    if not snippets:
        await lark_client.send_text_to_chat(http, chat_id, f"{_yesterday_title()}\n（昨天沒有可摘要的文字訊息）")
        return

    prompt = f"{SUMMARY_PROMPT}\n\n---\n聊天摘錄：\n" + "\n".join(f"- {s}" for s in snippets)
    summary = await openai_client.text_completion(prompt)
    await lark_client.send_text_to_chat(http, chat_id, summary)

async def run_daily_summary_per_chat():
    # 列出昨天有聊天的 chat_id，逐一發送
    start_dt, end_dt = utils.yesterday_range_local()
    start_ms, end_ms = utils.to_epoch_ms(start_dt), utils.to_epoch_ms(end_dt)
    async with AsyncSessionFactory() as db:
        chat_ids = await crud.get_chat_ids_with_messages_between(db, start_ms, end_ms)
    async with httpx.AsyncClient(timeout=30.0) as http:
        for cid in chat_ids:
            try:
                await summarize_for_single_chat(http, cid)
            except Exception:
                pass
