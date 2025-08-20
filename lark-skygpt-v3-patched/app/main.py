# app/main.py
import os
import json
import time
import asyncio
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
    _download_message_resource,  # 直接複用下載資源
)

logger = logging.getLogger("sky_lark")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# =========================
# Lark：取得租戶 Token 與發送訊息（輕量 helper，不干擾你原有 lark_client）
# =========================
LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_SEND_MESSAGE_URL = "https://open.larksuite.com/open-apis/im/v1/messages"

def _resolve_lark_credentials() -> tuple[str, str]:
    """
    兼容多種設定名稱與環境變數：
    settings.LARK_APP_ID / FEISHU_APP_ID / APP_ID（與對應 *_APP_SECRET），或同名環境變數。
    """
    app_id = (
        getattr(settings, "LARK_APP_ID", None)
        or getattr(settings, "FEISHU_APP_ID", None)
        or getattr(settings, "APP_ID", None)
        or os.getenv("LARK_APP_ID")
        or os.getenv("FEISHU_APP_ID")
        or os.getenv("APP_ID")
    )
    app_secret = (
        getattr(settings, "LARK_APP_SECRET", None)
        or getattr(settings, "FEISHU_APP_SECRET", None)
        or getattr(settings, "APP_SECRET", None)
        or os.getenv("LARK_APP_SECRET")
        or os.getenv("FEISHU_APP_SECRET")
        or os.getenv("APP_SECRET")
    )
    if not app_id or not app_secret:
        raise RuntimeError(
            "找不到 Lark 憑證，請設定 LARK_APP_ID/LARK_APP_SECRET"
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
    在群組/單聊回覆純文字訊息；失敗只記錄不拋錯，避免中斷 webhook 或排程。
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
@app.get("/")
async def root_ok():
    return {"ok": True, "service": "web", "env": getattr(settings, "ENV", "prod")}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "env": getattr(settings, "ENV", "prod")}

@app.get("/health")
async def health_alias():
    return await healthz()

# =========================
# 去重：避免同一 message_id 被重複處理（Lark 重試會重送）
# =========================
try:
    import redis.asyncio as aioredis  # 可選
except Exception:
    aioredis = None

REDIS_URL = getattr(settings, "REDIS_URL", None) or os.getenv("REDIS_URL") or os.getenv("REDIS_CONNECTION_URL")
_redis = aioredis.from_url(REDIS_URL) if (aioredis and REDIS_URL) else None
_local_seen: dict[str, float] = {}
_LOCAL_TTL = 24 * 3600

async def _dedupe_mark(message_id: str) -> bool:
    """
    True = 第一次看到，可處理；False = 已處理過，應跳過。
    優先用 Redis SETNX；沒有 Redis 則用本機字典降級。
    """
    key = f"lark:msg:{message_id}"
    if _redis:
        try:
            ok = await _redis.setnx(key, "1")
            if ok:
                await _redis.expire(key, _LOCAL_TTL)
                return True
            return False
        except Exception:
            pass
    now = time.time()
    # 清理過期
    for k, ts in list(_local_seen.items()):
        if now - ts > _LOCAL_TTL:
            _local_seen.pop(k, None)
    if key in _local_seen:
        return False
    _local_seen[key] = now
    return True

# =========================
# Lark Webhook（固定：/webhook/lark）
#   * 立即回 200，避免 499
#   * 背景任務處理（不阻塞）
# =========================
@app.post("/webhook/lark")
async def lark_webhook(request: Request):
    try:
        event = await request.json()
    except Exception:
        return JSONResponse({"msg": "ok"}, status_code=200)

    # URL Challenge
    if isinstance(event, dict) and "challenge" in event:
        return JSONResponse({"challenge": event["challenge"]}, status_code=200)

    # 背景處理，先回 200
    asyncio.create_task(_process_lark_event(event))
    return JSONResponse({"msg": "ok"}, status_code=200)

# 舊路由相容（如果後台還在用 /lark/webhook 也能通）
@app.post("/lark/webhook")
async def lark_webhook_alias(request: Request):
    return await lark_webhook(request)

# -------------------------
# 背景處理：實際 AI 路徑（文字 / 圖片 / PDF / DOCX / XLSX）
# -------------------------
async def _process_lark_event(event: Dict[str, Any]) -> None:
    header = event.get("header", {}) or {}
    if header.get("event_type") != "im.message.receive_v1":
        return

    ev = event.get("event", {}) or {}
    msg = ev.get("message", {}) or {}
    msg_type = msg.get("message_type")
    message_id = msg.get("message_id")
    chat_id = msg.get("chat_id")
    content_raw = msg.get("content", "{}")

    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else (content_raw or {})
    except Exception:
        content = {}

    if not chat_id:
        return

    # 去重：同一 message_id 只處理一次
    if message_id:
        first = await _dedupe_mark(message_id)
        if not first:
            return

    async with httpx.AsyncClient() as http:
        # 文字
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return
            try:
                if text in ("摘要", "總結", "总结", "summary"):
                    reply = await summarize_text_or_fallback(http, text)
                else:
                    reply = await reply_text_or_fallback(http, text)
            except Exception as e:
                logger.exception("處理文字訊息失敗：%s", e)
                reply = "(降級) 處理文字訊息時發生例外，已記錄日誌。"
            await reply_text(http, chat_id, reply, by_chat_id=True)
            return

        # 圖片
        if msg_type == "image":
            image_key = content.get("image_key")
            if not (message_id and image_key):
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或圖片 key，無法解析圖片。", by_chat_id=True)
                return
            try:
                result = await describe_image_from_message_or_fallback(http, message_id, image_key)
            except Exception as e:
                logger.exception("圖片 Vision 解析失敗：%s", e)
                result = f"(降級) 圖像解析異常：{e}"
            await reply_text(http, chat_id, result, by_chat_id=True)
            return

        # 檔案（PDF / DOCX / XLSX）
        if msg_type == "file":
            file_key = content.get("file_key")
            file_name = (content.get("file_name") or "").lower()
            if not (message_id and file_key):
                await reply_text(http, chat_id, "(降級) 未取得訊息 ID 或檔案 key，無法處理檔案。", by_chat_id=True)
                return

            # PDF → Vision（保留你原本流程）
            if file_name.endswith(".pdf"):
                try:
                    result = await describe_pdf_from_message_or_fallback(http, message_id, file_key)
                except Exception as e:
                    logger.exception("PDF Vision 解析失敗：%s", e)
                    result = f"(降級) PDF 解析異常：{e}"
                await reply_text(http, chat_id, result, by_chat_id=True)
                return

            # DOCX → 萃取文字 → 摘要
            if file_name.endswith(".docx"):
                try:
                    data = await _download_message_resource(http, message_id, file_key, rtype="file")
                    result = _extract_docx_text_and_summarize(data, http)
                except Exception as e:
                    logger.exception("DOCX 解析失敗：%s", e)
                    result = f"(降級) Word 解析異常：{e}"
                if asyncio.iscoroutine(result):
                    result = await result
                await reply_text(http, chat_id, result, by_chat_id=True)
                return

            # XLSX → 取前幾列文字 → 摘要
            if file_name.endswith(".xlsx"):
                try:
                    data = await _download_message_resource(http, message_id, file_key, rtype="file")
                    result = _extract_xlsx_text_and_summarize(data, http)
                except Exception as e:
                    logger.exception("XLSX 解析失敗：%s", e)
                    result = f"(降級) Excel 解析異常：{e}"
                if asyncio.iscoroutine(result):
                    result = await result
                await reply_text(http, chat_id, result, by_chat_id=True)
                return

            # 其他格式提示
            await reply_text(
                http,
                chat_id,
                f"(提示) 已接收檔案：{file_name or file_key}。目前支援 PDF / DOCX / XLSX 摘要。",
                by_chat_id=True,
            )
            return

        # 其他型別：提示未支援
        await reply_text(http, chat_id, f"收到類型 {msg_type}，暫未支援。", by_chat_id=True)

# -------------------------
# 檔案解析：DOCX / XLSX（轉成文字後交給 summarize_text_or_fallback）
# -------------------------
def _extract_docx_text(data: bytes) -> str:
    try:
        from io import BytesIO
        from docx import Document
        doc = Document(BytesIO(data))
        parts = []
        for p in doc.paragraphs:
            if p.text:
                parts.append(p.text)
        return "\n".join(parts).strip() or "(空白文件)"
    except Exception as e:
        return f"(降級) DOCX 解析失敗：{e}"

def _extract_xlsx_text(data: bytes, max_rows: int = 50, max_cols: int = 20) -> str:
    try:
        from io import BytesIO
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if r_idx > max_rows:
                rows.append("...（已截斷）")
                break
            cells = []
            for c_idx, v in enumerate(row, start=1):
                if c_idx > max_cols:
                    cells.append("…")
                    break
                cells.append("" if v is None else str(v))
            rows.append("\t".join(cells))
        header = f"[工作表：{ws.title}]"
        return header + "\n" + "\n".join(rows)
    except Exception as e:
        return f"(降級) XLSX 解析失敗：{e}"

async def _extract_docx_text_and_summarize(data: bytes, http: httpx.AsyncClient) -> str:
    text = _extract_docx_text(data)
    return await summarize_text_or_fallback(http, text)

async def _extract_xlsx_text_and_summarize(data: bytes, http: httpx.AsyncClient) -> str:
    text = _extract_xlsx_text(data)
    return await summarize_text_or_fallback(http, text)


