# app/tasks.py

import logging
import httpx
from datetime import datetime, timedelta
from . import crud, utils, openai_client, lark_client

logger = logging.getLogger(__name__)


async def summarize_for_single_chat(http: httpx.AsyncClient, chat_id: str):
    """
    從資料庫拉取昨天的聊天記錄，交給 OpenAI 產生摘要，並送回群組。
    """

    # 1) 抓昨天的訊息
    today = utils.now_local().date()
    yesterday = today - timedelta(days=1)
    start = datetime.combine(yesterday, datetime.min.time())
    end = datetime.combine(yesterday, datetime.max.time())
    msgs = await crud.get_messages_between(chat_id, start, end)

    if not msgs:
        await lark_client.send_text_to_chat(http, chat_id, f"昨日（{yesterday}）沒有聊天記錄。")
        return

    # 2) 拼接聊天記錄
    text = "\n".join([f"{m['text']}" for m in msgs if m.get("text")])

    # 3) Prompt 固定格式
    prompt = f"""
你是一個會議與聊天摘要專家，請幫我整理昨日聊天內容，輸出格式必須如下（保持中文，條列式）：

昨日聊天摘要 ({yesterday}):
 群組聊天記錄摘要

1. 關鍵決策：
   - 請列出昨天討論中做出的決策或結論
2. 待辦事項：
   - 請整理昨天分派的任務或工作
3. 未決問題：
   - 請列出還沒有結論、需要後續討論的議題
4. 其他資訊：
   - 其他有用的資訊或重點

以下是聊天記錄：
{text}
"""

    # 4) 丟給 OpenAI
    summary = await openai_client.summarize_text_or_fallback(http, prompt)

    # 5) 發回群組
    await lark_client.send_text_to_chat(http, chat_id, summary)


async def summarize_for_all_chats(http: httpx.AsyncClient):
    """
    對所有群組執行昨日摘要。
    """
    chats = await crud.get_all_chats()
    for chat in chats:
        try:
            await summarize_for_single_chat(http, chat["chat_id"])
        except Exception as e:
            logger.error(f"摘要失敗 chat={chat['chat_id']} error={e}")


async def record_message(event: dict):
    """
    儲存聊天訊息到資料庫。
    """
    try:
        chat_id = event["message"]["chat_id"]
        text = event["message"]["content"]["text"]
        sender = event["sender"]["sender_id"]["open_id"]
        timestamp = datetime.fromtimestamp(event["message"]["create_time"] / 1000.0)

        await crud.save_message(chat_id, sender, text, timestamp)
    except Exception as e:
        logger.error(f"記錄訊息失敗: {e}")
