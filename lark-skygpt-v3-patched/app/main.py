# app/main.py
import json
import logging
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import settings
from .openai_client import (
    reply_text_or_fallback,
    summarize_text_or_fallback,
    describe_image_from_message_or_fallback,
    describe_pdf_from_message_or_fallback,
)

logger = logging.getLogger("sky_lark")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# =========================
# Lark：取得租戶 Token 與發送訊息（內建 helper，避免相依問題）
# =========================
LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_SEND_MESSAGE_URL = "https://open.larksuite.com/open-apis/im/v1/messages"

def _resolve_lark_credentials() -> tuple[str, str]:
    """
    兼容多種設定名稱與環境變數：
    settings.LARK_APP_ID / FEISHU_APP_ID / APP_ID（與對應 *_APP_SECRET），或同名環境變數。
    """
    import os as _os
    app_id = (
        getattr(settings, "LARK_APP_ID", None)
        or getattr(settings, "FEISHU_APP_ID", None)
        or getattr(settings, "APP_ID", None)
        or _os.getenv("LARK_APP_ID")
        or _os.getenv("FEISHU_APP_ID")
        or _os.getenv("APP_ID")
    )
    app_secret = (
        getattr(settings, "LARK_APP_SECRET", None)
        or getattr(settings, "FEISHU_APP_SECRET", None)
        or getattr(settings, "APP_SECRET", None)
        or _os.getenv("LARK_APP_SECRET")
        or _os.getenv("FEISHU_APP_SECRET")
        or _os.getenv("APP_SECRET")
    )
    if not app_id or not app_secret:
        raise RuntimeError(
            "找不到 Lark 憑證，請設定 LARK_APP_ID/LARK_APP_SECRET "
            "（或 FEISHU_*、APP_*）於 settings 或環境變數。"
        )
    return app_id, app_secret

async def _get_tenant_access_token(http: httpx.AsyncClient) -> str:
    app_id, app_secret = _resolve_lark_credentials()
    r = await http.post(
        LARK_TENANT_TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    tok = data.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"取得 tenant_access_token 失敗：{data}")
    return tok

async def reply_text(http: httpx.AsyncClient, chat_id: str, text: str, *, by_chat_id: bool = True) -> None:
    """
    在群組/單聊回覆純文字訊息。
    失敗只記錄不拋出，避免中斷 webhook 或排程。
    """
    try:
        token = await _get_tenant_access_token(http)
    except Exception as e:
        logger.error("無法發送訊息，取得 token 失敗：%s", e)
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    params = {"receive_id_type": "chat_id" if by_chat_id else "open_id"}
    body = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}

    try:
        resp = await http.post(LARK_SEND_MESSAGE_URL, headers=headers, params=params, json=body, timeout=20)
        if resp.status_code >= 400:
            try:
                errtxt = (await resp.aread()).decode(errors="ignore")
            except Exception:
                errtxt = "<body read error>"
            logger.error("回覆訊息失敗 %s: %s", resp.status_code, errtxt)
    except Exception as e:
        logger.exception("回覆訊息發送異常：%s", e)

# =========================
# 健康檢查
# =========================
@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": settings.ENV}

# =========================
# Lark Webhook（文字 / 圖片 / PDF）
# =========================
@app.post("/lark/webhook")
async def lark_webhook(request: Request):
    """
    對齊 Lark 官方規範：
    - 圖片/檔案需透過【訊息資源】端點（在 openai_client 內部處理）
      GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image|file
    - 這裡負責解析事件、拆出 message_id / image_key / file_key 並調用對應 AI 函式。
    """
    try:
        event = await request.json()
    except Exception:
        return PlainTextResponse("ok")

    # 1) URL Challenge
    if "challenge" in event:
        return JSONResponse({"challenge": event["challenge"]})

    header = event.get("header", {}) or {}
    event_type = header.get("event_type")
    event_obj = event.get("event", {}) or {}

    # 僅處理訊息事件
    if event_type != "im.message.receive_v1":
        return PlainTextResponse("ok")

    # 2) 抽取訊息欄位
    msg = event_obj.get("message", {}) or {}
    msg_type = msg.get("message_type")
    message_id = msg.get("message_id")
    chat_id = msg.get("chat_id")
    content_raw = msg.get("content", "{}")

    # content 是 JSON 字串
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
    except Exception:
        content = {}

    if not chat_id:
        return PlainTextResponse("ok")

    async with httpx.AsyncClient() as http:
        # 文字：接 Chat 或 摘要
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return PlainTextResponse("ok")

            try:
                if text in ("摘要", "總結", "总结", "summary"):
                    reply = await summarize_text_or_fallback(http, text)
                else:
                    reply = await reply_text_or_fallback(http, text)
            except Exception as e:
                logger.exception("處理文字訊息失敗：%s", e)
                reply = "(降級) 處理文字訊息時發生例外，已記錄日誌。"

            await reply_text(http, chat_id, reply, by_chat_id=True)
            return PlainTextResponse("ok")

        # 圖片：需同時具有 message_id + image_key
        if msg_type == "image":
            image_key = content.get("image_key")
            if not message_id or not image_key:
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或圖片 key，無法解析圖片。", by_chat_id=True)
                return PlainTextResponse("ok")
            try:
                result = await describe_image_from_message_or_fallback(http, message_id, image_key)
            except Exception as e:
                logger.exception("圖片 Vision 解析失敗：%s", e)
                result = f"(降級) 圖像解析異常：{e}"
            await reply_text(http, chat_id, result, by_chat_id=True)
            return PlainTextResponse("ok")

        # 檔案（PDF）：message_id + file_key；只對 .pdf 走 Vision
        if msg_type == "file":
            file_key = content.get("file_key")
            file_name = (content.get("file_name") or "").lower()
            if not message_id or not file_key:
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或檔案 key，無法處理檔案。", by_chat_id=True)
                return PlainTextResponse("ok")

            if file_name.endswith(".pdf"):
                try:
                    result = await describe_pdf_from_message_or_fallback(http, message_id, file_key)
                except Exception as e:
                    logger.exception("PDF Vision 解析失敗：%s", e)
                    result = f"(降級) PDF 解析異常：{e}"
                await reply_text(http, chat_id, result, by_chat_id=True)
                return PlainTextResponse("ok")

            # 非 PDF：保留你原流程，這裡先友善提示
            await reply_text(
                http,
                chat_id,
                f"(提示) 已接收檔案：{file_name or file_key}。目前僅對 PDF 走 Vision，其他格式維持原流程。",
                by_chat_id=True,
            )
            return PlainTextResponse("ok")

        # 其他訊息型別：尚未支援
        await reply_text(http, chat_id, f"收到類型 {msg_type}，暫未支援。", by_chat_id=True)
        return PlainTextResponse("ok")
