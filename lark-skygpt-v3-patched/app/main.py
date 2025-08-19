# app/main.py
import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import settings
from .openai_client import (
    reply_text_or_fallback,
    summarize_text_or_fallback,
)

# 嘗試匯入：符合 Lark「訊息資源端點」的 Vision 函式
_HAS_IMAGE_VISION = False
_HAS_PDF_VISION = False
try:
    from .openai_client import describe_image_from_message_or_fallback  # type: ignore
    _HAS_IMAGE_VISION = True
except Exception:  # pragma: no cover
    describe_image_from_message_or_fallback = None  # type: ignore

try:
    from .openai_client import describe_pdf_from_message_or_fallback  # type: ignore
    _HAS_PDF_VISION = True
except Exception:  # pragma: no cover
    describe_pdf_from_message_or_fallback = None  # type: ignore

logger = logging.getLogger("sky_lark")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# =========================
# Lark：取得租戶 token + 回文字訊息（避免依賴 lark_client）
# =========================
LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_SEND_MESSAGE_URL = "https://open.larksuite.com/open-apis/im/v1/messages"


async def _get_tenant_access_token(http: httpx.AsyncClient) -> str:
    """
    以應用身份取得 tenant_access_token（自建/商店應用皆適用）
    """
    r = await http.post(
        LARK_TENANT_TOKEN_URL,
        json={"app_id": settings.LARK_APP_ID, "app_secret": settings.LARK_APP_SECRET},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    tok = data.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"取得 tenant_access_token 失敗：{data}")
    return tok


async def reply_text(http: httpx.AsyncClient, chat_id: str, text: str, by_chat_id: bool = True) -> None:
    """
    在群組/單聊回覆純文字訊息
    - 預設以 chat_id 發送（by_chat_id=True）
    """
    token = await _get_tenant_access_token(http)
    headers = {"Authorization": f"Bearer {token}"}
    params = {"receive_id_type": "chat_id" if by_chat_id else "open_id"}
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    resp = await http.post(LARK_SEND_MESSAGE_URL, headers=headers, params=params, json=body, timeout=20)
    if resp.status_code >= 400:
        # 只記錄，不拋錯，避免整體崩潰
        try:
            errtxt = (await resp.aread()).decode(errors="ignore")
        except Exception:
            errtxt = "<body read error>"
        logger.error("回覆訊息失敗 %s: %s", resp.status_code, errtxt)


# =========================
# 健康檢查
# =========================
@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": settings.ENV}


# =========================
# Webhook：對齊 Lark 官方事件
# =========================
@app.post("/lark/webhook")
async def lark_webhook(request: Request):
    """
    對齊 Lark 官方規範：
    - 圖片/檔案資源：必須使用「從訊息下載資源」端點（在 openai_client 內部處理）
      GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image|file
    - 本函式負責拆出 message_id、image_key/file_key 並呼叫 openai_client 的對應函式
    """
    try:
        event = await request.json()
    except Exception:
        # 無效 payload，直接 200
        return PlainTextResponse("ok")

    # 1) URL Challenge（Lark 訂閱驗證）
    if "challenge" in event:
        return JSONResponse({"challenge": event["challenge"]})

    header = event.get("header", {}) or {}
    event_type = header.get("event_type")
    event_obj = event.get("event", {}) or {}

    # 僅處理訊息事件
    if event_type != "im.message.receive_v1":
        return PlainTextResponse("ok")

    # 2) 取出訊息關鍵欄位
    msg = event_obj.get("message", {}) or {}
    msg_type = msg.get("message_type")
    message_id = msg.get("message_id")
    chat_id = msg.get("chat_id")  # 我們用 chat_id 回覆，避免跨群干擾
    content_raw = msg.get("content", "{}")

    # Lark content 是 JSON 字串
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
    except Exception:
        content = {}

    # 容錯：缺少 chat_id 或 message_id，直接返回
    if not chat_id:
        return PlainTextResponse("ok")

    async with httpx.AsyncClient() as http:
        # 3) 文字訊息：保留你原有語氣與摘要觸發詞
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return PlainTextResponse("ok")

            try:
                if text in ("摘要", "總結", "总结", "summary"):
                    reply = await summarize_text_or_fallback(http, f"對本群的歷史訊息做摘要（示意）。使用者輸入：{text}")
                else:
                    reply = await reply_text_or_fallback(http, text)
            except Exception as e:
                logger.exception("處理文字訊息失敗：%s", e)
                reply = "(降級) 處理文字訊息時發生例外，已記錄日志。"

            await reply_text(http, chat_id, reply, by_chat_id=True)
            return PlainTextResponse("ok")

        # 4) 圖片訊息：需同時具備 message_id + image_key
        if msg_type == "image":
            image_key = content.get("image_key")
            if not message_id or not image_key:
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或圖片 key，無法解析圖片。", by_chat_id=True)
                return PlainTextResponse("ok")

            if not _HAS_IMAGE_VISION or describe_image_from_message_or_fallback is None:
                # 尚未合入新版 Vision 函式
                await reply_text(
                    http,
                    chat_id,
                    "(降級) 尚未啟用圖片 Vision 解析，請先更新 openai_client.py（describe_image_from_message_or_fallback）。",
                    by_chat_id=True,
                )
                return PlainTextResponse("ok")

            try:
                result = await describe_image_from_message_or_fallback(http, message_id, image_key)  # type: ignore
            except Exception as e:
                logger.exception("圖片 Vision 解析失敗：%s", e)
                result = f"(降級) 圖像解析異常：{e}"

            await reply_text(http, chat_id, result, by_chat_id=True)
            return PlainTextResponse("ok")

        # 5) 檔案訊息：對 PDF 走 Vision（掃描 PDF 效果佳）
        if msg_type == "file":
            file_key = content.get("file_key")
            file_name = (content.get("file_name") or "").lower()
            if not message_id or not file_key:
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或檔案 key，無法處理檔案。", by_chat_id=True)
                return PlainTextResponse("ok")

            # 只對 .pdf 走 Vision，其它副檔名維持你既有流程（此處給提示）
            if file_name.endswith(".pdf"):
                if not _HAS_PDF_VISION or describe_pdf_from_message_or_fallback is None:
                    await reply_text(
                        http,
                        chat_id,
                        "(降級) 尚未啟用 PDF→圖片→Vision 解析，請先更新 openai_client.py（describe_pdf_from_message_or_fallback）。",
                        by_chat_id=True,
                    )
                    return PlainTextResponse("ok")

                try:
                    result = await describe_pdf_from_message_or_fallback(http, message_id, file_key)  # type: ignore
                except Exception as e:
                    logger.exception("PDF Vision 解析失敗：%s", e)
                    result = f"(降級) PDF 解析異常：{e}"

                await reply_text(http, chat_id, result, by_chat_id=True)
                return PlainTextResponse("ok")

            # 非 PDF：不走 Vision，回提示（不破壞你現有流程）
            await reply_text(
                http,
                chat_id,
                f"(提示) 已接收檔案：{file_name or file_key}。目前僅對 PDF 走 Vision，其他格式維持你原有流程。",
                by_chat_id=True,
            )
            return PlainTextResponse("ok")

        # 6) 其他訊息型別（例如音訊/影片/表情包等）
        await reply_text(http, chat_id, f"收到類型 {msg_type}，尚未支援，將持續擴充。", by_chat_id=True)
        return PlainTextResponse("ok")
