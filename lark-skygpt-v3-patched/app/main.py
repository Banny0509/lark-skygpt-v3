# app/main.py
import json
import logging
from typing import Dict, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import settings
from .lark_client import reply_text  # 延用你現有的回覆函式（by_chat_id=True）
from .openai_client import (
    reply_text_or_fallback,
    summarize_text_or_fallback,
    describe_image_from_message_or_fallback,   # ✅ 新：圖片走訊息資源端點
    describe_pdf_from_message_or_fallback,     # ✅ 新：PDF 走訊息資源端點 → 轉圖 → Vision
)

logger = logging.getLogger("sky_lark")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": settings.ENV}

@app.post("/lark/webhook")
async def lark_webhook(request: Request):
    """
    對齊 Lark 官方規範：
    - 圖片與文件「必須」透過訊息資源端點下載：
      GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image|file
    - 因此在處理 image/file 時務必同時取得 message_id 與 image_key/file_key
    """
    event = await request.json()

    # 1) URL Challenge
    if "challenge" in event:
        return JSONResponse({"challenge": event["challenge"]})

    header = event.get("header", {})
    event_type = header.get("event_type")
    event_obj = event.get("event", {})

    # 僅處理訊息事件
    if event_type != "im.message.receive_v1":
        return PlainTextResponse("ok")

    # 2) 取出關鍵欄位
    msg = event_obj.get("message", {}) or {}
    msg_type = msg.get("message_type")
    message_id = msg.get("message_id")  # ✅ Lark 規範要求
    chat_id = msg.get("chat_id")
    content_raw = msg.get("content", "{}")

    # Lark 會把 content 存成 JSON 字串
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
    except Exception:
        content = {}

    async with httpx.AsyncClient() as http:
        # 3) 文字訊息：保留你原有語氣與摘要關鍵詞
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return PlainTextResponse("ok")

            # 即時摘要的觸發詞（保持你原設計）
            if text in ("摘要", "總結", "总结", "summary"):
                reply = await summarize_text_or_fallback(http, f"對本群的歷史訊息做摘要（示意）。使用者輸入：{text}")
            else:
                reply = await reply_text_or_fallback(http, text)

            await reply_text(http, chat_id, reply, by_chat_id=True)
            return PlainTextResponse("ok")

        # 4) 圖片訊息：依 Lark 規範，必須從「訊息資源」端點下載
        #    content 典型格式：{"image_key": "img_v2_xxx"}；並且 message_id 必須同時帶上
        if msg_type == "image":
            image_key = content.get("image_key")
            if not (message_id and image_key):
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或圖片 key，無法解析圖片。", by_chat_id=True)
                return PlainTextResponse("ok")

            result = await describe_image_from_message_or_fallback(http, message_id, image_key)
            await reply_text(http, chat_id, result, by_chat_id=True)
            return PlainTextResponse("ok")

        # 5) 檔案訊息：對 PDF 走「訊息資源：type=file」→ 轉圖 → Vision
        #    content 典型格式：{"file_key": "file_v2_xxx", "file_name": "xxx.pdf", ...}
        if msg_type == "file":
            file_key = content.get("file_key")
            file_name = (content.get("file_name") or "").lower()
            if not (message_id and file_key):
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或檔案 key，無法處理檔案。", by_chat_id=True)
                return PlainTextResponse("ok")

            # 只針對 PDF 走 Vision（掃描 PDF 成效更好）
            if file_name.endswith(".pdf"):
                result = await describe_pdf_from_message_or_fallback(http, message_id, file_key)
                await reply_text(http, chat_id, result, by_chat_id=True)
                return PlainTextResponse("ok")

            # 其他非 PDF：沿用你既有流程（若你已有純文字抽取與摘要，請保留）
            # 這裡給一個友善提示，避免服務崩潰
            await reply_text(
                http,
                chat_id,
                f"(提示) 已接收檔案：{file_name or file_key}。目前僅對 PDF 支援 Vision 解析，其他格式維持原流程。",
                by_chat_id=True,
            )
            return PlainTextResponse("ok")

        # 6) 其他訊息型別：回覆尚未支援
        await reply_text(http, chat_id, f"收到類型 {msg_type}，尚未支援，將持續擴充。", by_chat_id=True)
        return PlainTextResponse("ok")
