
# app/main.py
import json
import logging
from typing import Dict, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import settings
from .openai_client import chat, summarize
from .lark_client import reply_text, download_and_extract_text

logger = logging.getLogger("sky_lark")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": settings.ENV}

@app.post("/lark/webhook")
async def lark_webhook(request: Request):
    event = await request.json()
    if "challenge" in event:
        return JSONResponse({"challenge": event["challenge"]})

    header = event.get("header", {})
    event_type = header.get("event_type")
    event_obj = event.get("event", {})

    async with httpx.AsyncClient() as http:
        if event_type == "im.message.receive_v1":
            msg = event_obj.get("message", {})
            msg_type = msg.get("message_type")
            content_raw = msg.get("content", "{}")
            try:
                content = json.loads(content_raw)
            except Exception:
                content = {"text": content_raw}

            chat_id = msg.get("chat_id")
            open_id = msg.get("sender", {}).get("sender_id", {}).get("open_id")

            if msg_type == "text":
                text = (content.get("text") or "").strip()
                if not text:
                    return PlainTextResponse("ok")

                if text in ("摘要", "總結", "总结", "summary"):
                    reply = summarize(f"对本群的历史消息做摘要（示意）。用户输入：{text}")
                else:
                    messages = [
                        {"role": "system", "content": "你是说中文的企业助理，语气专业、精炼。"},
                        {"role": "user", "content": text},
                    ]
                    reply = chat(messages)

                await reply_text(http, chat_id, reply, by_chat_id=True)
                return PlainTextResponse("ok")

            if msg_type in ("file", "image"):
                file_key = content.get("file_key") or content.get("image_key")
                reply = "未取得文件信息"
                if file_key:
                    extracted = await download_and_extract_text(http, file_key)
                    reply = summarize(extracted)
                await reply_text(http, chat_id, reply, by_chat_id=True)
                return PlainTextResponse("ok")

            await reply_text(http, chat_id, f"收到类型 {msg_type}，尚未支持，将持续扩充。", by_chat_id=True)
            return PlainTextResponse("ok")

        return PlainTextResponse("ok")
