# app/tasks.py
import logging
from typing import Optional
import httpx
from openai import AsyncOpenAI
from sqlalchemy import select

# 匯入專案模組
from . import lark_client, crud, utils
from .config import settings
from .database import AsyncSessionFactory, Message

# 使用與 web/worker 一致的日誌記錄器
logger = logging.getLogger("web") or logging.getLogger("worker")

# --- 初始化 OpenAI 客戶端 ---
aclient: Optional[AsyncOpenAI] = None
if settings.OPENAI_API_KEY:
    try:
        aclient = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        logger.info("OpenAI 客戶端 (for tasks) 初始化成功。")
    except Exception as e:
        logger.error(f"OpenAI 客戶端 (for tasks) 初始化失敗: {e}")
else:
    logger.warning("未設定 OPENAI_API_KEY，每日摘要功能將無法運作。")

async def summarize_for_all_chats(http_client: httpx.AsyncClient):
    """
    為所有在昨天有新訊息的群組產生摘要。
    這是由 scheduler_worker.py 定時呼叫的函式。
    """
    if not aclient:
        logger.warning("未設定 OPENAI_API_KEY，無法執行每日摘要。")
        return

    start_dt, end_dt = utils.yesterday_range_local()
    start_ms = utils.to_epoch_ms(start_dt)
    end_ms = utils.to_epoch_ms(end_dt)
    date_str = start_dt.strftime("%Y-%m-%d")
    logger.info(f"開始執行 {date_str} 的每日摘要任務...")

    async with AsyncSessionFactory() as db:
        # 1. 找出昨天有哪些 chat_id 活躍過
        chat_ids = await crud.get_chat_ids_with_messages_between(db, start_ms, end_ms)
        logger.info(f"發現 {len(chat_ids)} 個活躍的聊天室需要摘要。")

        for chat_id in chat_ids:
            # 2. 使用資料庫鎖，避免多個 worker 重複執行同一個摘要
            is_locked = await crud.acquire_summary_lock(db, date_str, chat_id)
            if not is_locked:
                logger.warning(f"Chat {chat_id} 的 {date_str} 摘要任務已被鎖定或已完成，跳過。")
                continue

            try:
                # 3. 從資料庫撈取昨天的所有文字訊息
                q = select(Message.text).where(
                    Message.chat_id == chat_id,
                    Message.ts_ms >= start_ms,
                    Message.ts_ms < end_ms,
                    Message.text.isnot(None),
                    Message.text != ""
                ).order_by(Message.ts_ms)
                
                res = await db.execute(q)
                messages = [r[0] for r in res.fetchall()]
                
                if len(messages) < 5: # 訊息太少，沒有摘要的必要
                    logger.info(f"Chat {chat_id} 訊息過少 ({len(messages)}則)，跳過摘要。")
                    continue

                # 4. 呼叫 OpenAI 產生摘要
                logger.info(f"正在為 Chat {chat_id} 產生摘要 ({len(messages)}則訊息)...")
                summary_prompt = (
                    "你是一位專業的會議記錄員，請根據以下聊天紀錄，整理出一份簡潔、清晰、條列式的繁體中文摘要，並忽略無關緊要的閒聊。\n"
                    f"摘要的開頭必須是：「以下是 {date_str} 的對話摘要：」\n\n"
                    "聊天紀錄如下：\n---\n"
                    + "\n".join(f"- {m}" for m in messages)
                )

                chat_completion = await aclient.chat.completions.create(
                    messages=[{"role": "user", "content": summary_prompt}],
                    model="gpt-4o-mini",
                    temperature=0.2,
                    timeout=180,
                )
                summary_text = chat_completion.choices[0].message.content.strip()

                # 5. 將摘要發送到對應的聊天室
                await lark_client.send_message(http_client, chat_id, summary_text)
                logger.info(f"成功發送 Chat {chat_id} 的每日摘要。")

            except Exception as e:
                logger.exception(f"處理 Chat {chat_id} 的摘要時發生錯誤: {e}")
                error_notice = f"抱歉，在產生 {date_str} 的摘要時發生內部錯誤，請聯繫管理員。"
                await lark_client.send_message(http_client, chat_id, error_notice)
