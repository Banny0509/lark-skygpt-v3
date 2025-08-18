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
# 這樣在 Railway 上查看日誌時，來源才會一致
logger = logging.getLogger("worker")

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

                # 4. 組合聊天紀錄文字
                raw_text = "\n".join([f"- {m}" for m in messages])

                # --- 整合您指定的 Prompt 格式 ---
                summary_prompt = f"""
你是一個會議與聊天摘要專家，請幫我整理昨日聊天內容，輸出格式必須如下（保持繁體中文，條列式）：

昨日聊天摘要 ({date_str})
群組聊天記錄摘要

1. 關鍵決策：
   - 請列出昨天討論中做出的決策或結論，如果沒有則寫「無」。
2. 待辦事項：
   - 請整理昨天分派的任務或工作，如果沒有則寫「無」。
3. 未決問題：
   - 請列出還沒有結論、需要後續討論的議題，如果沒有則寫「無」。
4. 其他資訊：
   - 其他有用的資訊或重點，如果沒有則寫「無」。

---
以下是聊天記錄原文：
{raw_text}
"""

                # 5. 呼叫 OpenAI 產生摘要
                logger.info(f"正在為 Chat {chat_id} 產生摘要 ({len(messages)}則訊息)...")
                chat_completion = await aclient.chat.completions.create(
                    messages=[{"role": "user", "content": summary_prompt}],
                    model="gpt-4o-mini", # 您也可以考慮使用 gpt-4-turbo 來獲得更好的長文本理解能力
                    temperature=0.2,
                    timeout=180,
                )
                summary_text = chat_completion.choices[0].message.content.strip()

                # 6. 將摘要發送到對應的聊天室
                await lark_client.send_message(http_client, chat_id, summary_text)
                logger.info(f"成功發送 Chat {chat_id} 的每日摘要。")

            except Exception as e:
                logger.exception(f"處理 Chat {chat_id} 的摘要時發生錯誤: {e}")
                error_notice = f"抱歉，在產生 {date_str} 的摘要時發生內部錯誤，請聯繫管理員。"
                await lark_client.send_message(http_client, chat_id, error_notice)
