import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import httpx
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import init_db, AsyncSessionFactory
from . import crud, lark_client, openai_client, utils, tasks

logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger("skygpt-web")
shared_state: Dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting...")
    await init_db()
    shared_state["http"] = httpx.AsyncClient(timeout=30.0)
    logger.info("SkyGPT web service ready.")
    yield
    await shared_state["http"].aclose()
    logger.info("HTTP client closed.")

app = FastAPI(title="Lark SkyGPT Bot", lifespan=lifespan)

async def get_db() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        yield session

def _parse_msg_basic(msg: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = msg.get("chat_id")
    message_id = msg.get("message_id")
    msg_type = msg.get("message_type")
    chat_type = msg.get("chat_type")  # 'p2p' or 'group'
    sender = msg.get("sender") or {}
    sender_open_id = (sender.get("sender_id") or {}).get("open_id") or ""
    try:
        create_ms = int(msg.get("create_time") or "0")
    except Exception:
        create_ms = 0
    content_str = msg.get("content") or "{}"
    try:
        content = json.loads(content_str)
    except Exception:
        content = {}
    return dict(chat_id=chat_id, message_id=message_id, msg_type=msg_type, chat_type=chat_type,
                sender_id=sender_open_id, ts_ms=create_ms, content=content)

@app.post("/webhook/lark")
async def lark_event(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    if "challenge" in body:
        return JSONResponse({"challenge": body["challenge"]})

    event = body.get("event", {}) or {}
    header = body.get("header", {}) or {}
    etype = header.get("event_type") or event.get("type")
    http = shared_state["http"]

    if etype and "message" in etype:
        msg = event.get("message") or {}
        meta = _parse_msg_basic(msg)

        # DB 保存（best-effort）
        try:
            await crud.insert_message(db, {
                "chat_id": meta["chat_id"],
                "message_id": meta["message_id"],
                "sender_id": meta["sender_id"],
                "ts_ms": meta["ts_ms"],
                "msg_type": meta["msg_type"],
                "text": meta["content"].get("text") if meta["msg_type"] == "text" else None,
                "file_key": meta["content"].get("file_key") if meta["msg_type"] == "file" else None,
                "image_key": meta["content"].get("image_key") if meta["msg_type"] == "image" else None,
            })
        except Exception:
            pass

        chat_id = meta["chat_id"]
        msg_type = meta["msg_type"]
        chat_type = meta["chat_type"]
        content = meta["content"]

        # --- text ---
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return JSONResponse({"code":0})

            # 群組需 @BOT_NAME 才回覆（檔案/圖片除外）
            if chat_type == "group":
                is_mentioned = False
                clean_text = text
                mentions = msg.get("mentions") or []
                for m in mentions:
                    if m.get("name") == settings.BOT_NAME:
                        is_mentioned = True
                        key_to_remove = m.get("key","")
                        clean_text = text.replace(key_to_remove, "").strip()
                        break
                if not is_mentioned:
                    return JSONResponse({"code":0})
                text_to_process = clean_text
            else:
                text_to_process = text

            # commands
            if text_to_process.startswith("/help"):
                await lark_client.send_text_to_chat(http, chat_id,
                    "指令：\n/time 現在時間\n/date 今日日期\n/summary 立即彙整昨天摘要（只此群）")
                return JSONResponse({"code":0})
            if text_to_process.startswith("/time"):
                await lark_client.send_text_to_chat(http, chat_id, utils.now_local().strftime("現在時間：%Y-%m-%d %H:%M:%S %Z"))
                return JSONResponse({"code":0})
            if text_to_process.startswith("/date"):
                await lark_client.send_text_to_chat(http, chat_id, utils.now_local().strftime("今日日期：%Y-%m-%d（%A）"))
                return JSONResponse({"code":0})
            if text_to_process.startswith("/summary"):
                await tasks.summarize_for_single_chat(http, chat_id)
                return JSONResponse({"code":0})

            prompt = f"現在本地時間是 {utils.now_local().strftime('%Y-%m-%d %H:%M:%S %Z')}。\n請用繁體中文回答：\n\n使用者：{text_to_process}"
            out = await openai_client.text_completion(prompt)
            await lark_client.send_text_to_chat(http, chat_id, out)
            return JSONResponse({"code":0})

        # --- image ---
        if msg_type == "image":
            image_key = content.get("image_key")
            if not image_key:
                await lark_client.send_text_to_chat(http, chat_id, "收到圖片，但缺少 image_key。")
                return JSONResponse({"code":0})
            try:
                img, _, _ = await lark_client.download_message_resource(http, meta["message_id"], image_key, "image")
                desc = await openai_client.vision_describe(img)
                await lark_client.send_text_to_chat(http, chat_id, desc)
            except Exception:
                await lark_client.send_text_to_chat(http, chat_id, "圖片下載/解析失敗，請稍後再試。")
            return JSONResponse({"code":0})

        # --- file ---
        if msg_type == "file":
            file_key = content.get("file_key"); file_name = content.get("file_name") or "file"
            if not file_key:
                await lark_client.send_text_to_chat(http, chat_id, "收到檔案，但缺少 file_key。")
                return JSONResponse({"code":0})
            try:
                data, header_name, content_type = await lark_client.download_message_resource(http, meta["message_id"], file_key, "file")
                fname = utils.guess_filename(file_name, content_type, header_name)
                text = utils.extract_text_generic(data, fname, content_type)
                prompt = f"以下是使用者上傳文件「{fname}」的內容摘錄，請以繁體中文摘要重點與待辦：\n\n{text[:12000]}"
                out = await openai_client.text_completion(prompt)
                await lark_client.send_text_to_chat(http, chat_id, out)
            except Exception:
                await lark_client.send_text_to_chat(http, chat_id, "檔案下載/解析失敗，請稍後再試。")
            return JSONResponse({"code":0})

    return JSONResponse({"code": 0})

@app.get("/")
async def root_ok():
    return PlainTextResponse("ok")

@app.get("/healthz")
async def healthz():
    return JSONResponse({
        "status":"ok",
        "now": utils.now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "tz": settings.TIMEZONE,
        "openai": bool(settings.OPENAI_API_KEY),
    })
