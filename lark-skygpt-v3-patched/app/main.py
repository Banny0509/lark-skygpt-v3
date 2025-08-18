from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, BackgroundTasks
from sqlalchemy import text as sql_text

from .config import settings
from .database import init_db, AsyncSessionFactory
from .crud import insert_message
from .lark_client import LarkClient
from .utils import pdf_bytes_to_text
from .openai_client import summarize_text

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(title="SkyGPT Web")

lark = LarkClient()

@app.on_event("startup")
async def _startup():
    await init_db()
    logger.info("startup ok")

@app.get("/")
async def health():
    return {"ok": True, "name": settings.BOT_NAME}

@app.get("/db-ping")
async def db_ping():
    async with AsyncSessionFactory() as s:
        r = await s.execute(sql_text("SELECT 1"))
        return {"ok": (r.scalar() == 1)}

# -------- Webhook (事件訂閱) --------
@app.post("/webhook")
async def lark_webhook(req: Request, bg: BackgroundTasks):
    body: Dict[str, Any] = await req.json()

    # 1) URL 驗證
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    header = body.get("header") or {}
    event = body.get("event") or {}

    event_type = header.get("event_type") or event.get("type")
    if not event_type:
        return {"code": 0}

    # 2) 只關注消息事件
    if event_type not in {"im.message.receive_v1"}:
        return {"code": 0}

    msg = event.get("message") or {}
    msg_type = msg.get("msg_type")
    message_id = msg.get("message_id")
    chat_id = msg.get("chat_id")
    chat_type = msg.get("chat_type")  # p2p / group
    sender_id = (msg.get("sender") or {}).get("sender_id") or None
    ts = int(msg.get("create_time") or 0)

    # content 是 JSON 字串
    content_raw = msg.get("content") or "{}"
    try:
        content = json.loads(content_raw)
    except Exception:
        content = {}

    text = None
    file_key = None
    image_key = None

    if msg_type == "text":
        text = content.get("text")
    elif msg_type == "file":
        file_key = content.get("file_key")
    elif msg_type == "image":
        image_key = content.get("image_key")

    # 3) 入庫（避免衝突已在 crud 內處理）
    async with AsyncSessionFactory() as s:
        await insert_message(
            s,
            chat_id=chat_id,
            message_id=message_id,
            ts_ms=ts,
            chat_type=chat_type,
            msg_type=msg_type,
            text=text,
            file_key=file_key,
            image_key=image_key,
            sender_id=sender_id,
        )

    # 4) 立即回覆「收到」以改善體驗
    try:
        if msg_type == "file":
            lark.send_text(chat_id, f"已收到文件（{message_id}），正在分析，請稍候…")
        elif msg_type == "image":
            lark.send_text(chat_id, f"已收到圖片（{message_id}），正在解析，請稍候…")
    except Exception as e:
        logger.warning("send ack failed: %s", e)

    # 5) 背景處理（不阻塞 webhook）
    bg.add_task(process_message_background, message_id, chat_id, msg_type, file_key, image_key)

    # 6) 立即結束（Lark 需 1s 內回應）
    return {"code": 0}

# ---- 背景任務：取檔 → 解析 → OpenAI → 回覆 ----
def process_message_background(message_id: str, chat_id: str, msg_type: str, file_key: str | None, image_key: str | None):
    try:
        if msg_type == "file" and file_key:
            # 取「消息中的資源文件」：type=file
            data, filename = lark.get_message_resource(message_id, file_key, typ="file")
            # 目前僅處理 PDF
            if not (filename or "").lower().endswith(".pdf"):
                lark.send_text(chat_id, f"收到的檔案不是 PDF（{filename or 'unknown'}），目前僅支援 PDF 摘要。")
                return
            text, pages = pdf_bytes_to_text(data, max_pages=30)
            if not text.strip():
                lark.send_text(chat_id, "PDF 文字內容為空，無法解析。")
                return
            summary = summarize_text(text, filename=filename or "document.pdf")
            lark.send_text(chat_id, f"《{filename or '文件'}》摘要：\n{summary}")

        elif msg_type == "image" and image_key:
            # 如需圖像理解，可擴充這裡為 Vision；暫時回覆不支援
            lark.send_text(chat_id, "目前優先支援 PDF 文件摘要；圖片解析將在後續版本提供。")

        elif msg_type == "text":
            # 可在此接一般對話
            pass

    except Exception as e:
        logger.exception("process_message_background error: %s", e)
        try:
            lark.send_text(chat_id, f"分析失敗：{e}")
        except Exception:
            pass
