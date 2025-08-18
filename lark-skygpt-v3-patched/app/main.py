# app/main.py
import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, List

import httpx
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import init_db, AsyncSessionFactory
from . import crud, lark_client, openai_client, utils, tasks

# -----------------------------
# 行為開關（群聊是否所有文字都回覆）
# 在 Railway Variables 可設 RESPOND_ALL_GROUP_TEXT=true/false
# 我這裡預設 True 以符合你的需求「除了 /help 以外全部走 OpenAI」
# -----------------------------
RESPOND_ALL_GROUP_TEXT = (os.getenv("RESPOND_ALL_GROUP_TEXT", "true").lower() == "true")

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
logger = logging.getLogger("skygpt-web")

# -----------------------------
# 應用生命週期
# -----------------------------
shared_state: Dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    http = httpx.AsyncClient(timeout=30.0)
    shared_state["http"] = http
    logger.info(
        "Web app started. TZ=%s BOT_NAME=%s RESPOND_ALL_GROUP_TEXT=%s",
        settings.TIMEZONE, settings.BOT_NAME, RESPOND_ALL_GROUP_TEXT
    )
    try:
        yield
    finally:
        try:
            await http.aclose()
        except Exception:
            pass
        logger.info("Web app stopped.")

app = FastAPI(lifespan=lifespan)

# -----------------------------
# 健康檢查 / 根路由
# -----------------------------
@app.get("/")
async def root_ok():
    return PlainTextResponse("ok")

@app.get("/healthz")
async def healthz():
    return JSONResponse({
        "status": "ok",
        "now": utils.now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "tz": settings.TIMEZONE,
        "openai": bool(settings.OPENAI_API_KEY),
    })

# -----------------------------
# Webhook：Lark 事件入口
# -----------------------------
@app.post("/webhook/lark")
async def lark_event(request: Request, db: AsyncSession = Depends(lambda: AsyncSessionFactory())):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"code": 0})

    # 1) challenge 驗證
    if isinstance(body, dict) and "challenge" in body:
        return JSONResponse({"challenge": body["challenge"]})

    event = body.get("event", {}) or {}
    header = body.get("header", {}) or {}
    etype = header.get("event_type") or event.get("type")
    http: httpx.AsyncClient = shared_state["http"]

    # 僅處理 message 類事件
    if not (etype and "message" in etype):
        return JSONResponse({"code": 0})

    # 2) 解析訊息基礎資訊
    msg = event.get("message") or {}
    meta = _parse_msg_basic(msg)
    chat_id = meta["chat_id"]
    message_id = meta["message_id"]
    msg_type = meta["msg_type"]
    chat_type = meta["chat_type"]
    content = meta["content"]
    create_ms = meta["create_ms"]

    # 3) 儲存到 DB（best-effort）
    try:
        await crud.insert_message(db, {
            "chat_id": chat_id,
            "message_id": message_id,
            "ts_ms": create_ms,
            "msg_type": msg_type,
            "chat_type": chat_type,
            "text": content.get("text"),
            "file_key": content.get("file_key"),
            "image_key": content.get("image_key"),
        })
        await db.commit()
    except Exception:
        await db.rollback()

    # 4) 分類處理
    try:
        # ---- 純文字 ----
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return JSONResponse({"code": 0})

            # /help：列出指令
            if text.startswith("/help"):
                await lark_client.send_text_to_chat(http, chat_id,
                    "指令：\n"
                    "/time 現在時間\n"
                    "/date 今日日期\n"
                    "/summary 立即彙整昨天摘要（只此群）\n"
                    "其他任何訊息將由 AI 回覆。"
                )
                return JSONResponse({"code": 0})

            # 其餘全部走 OpenAI（但你也要能用 /time /date /summary）
            if text.startswith("/time"):
                await lark_client.send_text_to_chat(
                    http, chat_id, utils.now_local().strftime("現在時間：%Y-%m-%d %H:%M:%S %Z")
                )
                return JSONResponse({"code": 0})

            if text.startswith("/date"):
                await lark_client.send_text_to_chat(
                    http, chat_id, utils.now_local().strftime("今日日期：%Y-%m-%d（%A）")
                )
                return JSONResponse({"code": 0})

            if text.startswith("/summary"):
                try:
                    await tasks.summarize_for_single_chat(http, chat_id)
                except Exception as e:
                    logger.exception("summary failed: %s", e)
                    await lark_client.send_text_to_chat(http, chat_id, "摘要失敗，請稍後再試。")
                return JSONResponse({"code": 0})

            # ---- 群聊門檻控制 ----
            proceed = True
            if chat_type == "group" and not RESPOND_ALL_GROUP_TEXT:
                # 安靜模式下，僅在被 @ 時回覆
                if not _is_bot_mentioned(msg, settings.BOT_NAME):
                    return JSONResponse({"code": 0})
                text = _strip_bot_mention(text, settings.BOT_NAME)

            # ---- 丟給 OpenAI ----
            reply = await openai_client.reply_text_or_fallback(http, text)
            await lark_client.send_text_to_chat(http, chat_id, reply)
            return JSONResponse({"code": 0})

        # ---- 圖片 ----
        if msg_type == "image":
            image_key = content.get("image_key")
            if not image_key:
                return JSONResponse({"code": 0})
            desc = await openai_client.describe_image_or_fallback(http, image_key)
            await lark_client.send_text_to_chat(http, chat_id, desc)
            return JSONResponse({"code": 0})

        # ---- 檔案 ----
        if msg_type == "file":
            file_key = content.get("file_key")
            file_name = content.get("file_name") or ""
            if not file_key:
                return JSONResponse({"code": 0})
            text = await lark_client.download_and_extract_text(http, file_key, file_name)
            summary = await openai_client.summarize_text_or_fallback(http, text)
            await lark_client.send_text_to_chat(http, chat_id, summary)
            return JSONResponse({"code": 0})

    except Exception as e:
        logger.exception("handle message failed: %s", e)

    return JSONResponse({"code": 0})

# -----------------------------
# 小工具
# -----------------------------
def _parse_msg_basic(msg: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = msg.get("chat_id")
    message_id = msg.get("message_id")
    msg_type = msg.get("message_type")
    chat_type = msg.get("chat_type")  # 'p2p' or 'group'
    try:
        create_ms = int(msg.get("create_time") or "0")
    except Exception:
        create_ms = 0

    content_str = msg.get("content") or "{}"
    try:
        content = json.loads(content_str)
    except Exception:
        content = {}

    if msg_type == "image":
        image_key = msg.get("image_key") or (content.get("image_key") if isinstance(content, dict) else None)
        content = {"image_key": image_key}
    elif msg_type == "file":
        file_key = msg.get("file_key") or (content.get("file_key") if isinstance(content, dict) else None)
        file_name = msg.get("file_name") or (content.get("file_name") if isinstance(content, dict) else None)
        content = {"file_key": file_key, "file_name": file_name}

    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "msg_type": msg_type,
        "chat_type": chat_type,
        "create_ms": create_ms,
        "content": content if isinstance(content, dict) else {},
    }

def _normalize_name(name: str) -> str:
    if not name:
        return ""
    return name.replace("@", "").replace(" ", "").strip().casefold()

def _is_bot_mentioned(msg: Dict[str, Any], bot_name: str) -> bool:
    mentions: List[Dict[str, Any]] = msg.get("mentions") or []
    if not mentions:
        return False
    want = _normalize_name(bot_name)
    for m in mentions:
        nm = _normalize_name(m.get("name") or "")
        if nm and nm == want:
            return True
        key = m.get("key") or ""
        if key.startswith("@") and _normalize_name(key) == want:
            return True
    return False

def _strip_bot_mention(text: str, bot_name: str) -> str:
    if not text:
        return text
    t = text.lstrip()
    norm = _normalize_name(bot_name)
    if t.startswith("@"):
        parts = t.split(" ", 1)
        if parts:
            leading = _normalize_name(parts[0])
            if leading.startswith(norm):
                return parts[1] if len(parts) > 1 else ""
    return text
