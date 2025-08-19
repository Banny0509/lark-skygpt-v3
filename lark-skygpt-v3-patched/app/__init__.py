# app/__init__.py
"""
套件層相容補丁：
- 不修改你的 app/lark_client.py
- 在 import app 時，若 lark_client 缺少 get_tenant_access_token / send_text / reply_text，
  這裡會動態補上，避免 Worker 啟動時 AttributeError。
"""

from __future__ import annotations

import os
import json
import logging
from typing import Optional

import httpx

# 匯出子模組（保持你原有行為）
from . import lark_client  # type: ignore
from . import config  # 確保 settings 可用
from .config import settings  # type: ignore

logger = logging.getLogger(__name__)

# Lark 端點
_LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
_LARK_SEND_MESSAGE_URL = "https://open.larksuite.com/open-apis/im/v1/messages"

# ---- 授權資訊解析：同時支援 LARK_* / FEISHU_* / APP_* 與環境變數 ----
def _resolve_lark_credentials() -> tuple[str, str]:
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
            "找不到 Lark 應用憑證，請設定 LARK_APP_ID/LARK_APP_SECRET "
            "（或 FEISHU_*、APP_*）於 settings 或環境變數。"
        )
    return app_id, app_secret


# ---- 這裡定義 shim，僅在 lark_client 缺少對應屬性時注入 ----
async def _shim_get_tenant_access_token(http: httpx.AsyncClient) -> str:
    app_id, app_secret = _resolve_lark_credentials()
    r = await http.post(
        _LARK_TENANT_TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    tok = data.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"取得 tenant_access_token 失敗：{data}")
    return tok


async def _shim_send_text(http: httpx.AsyncClient, chat_id: str, text: str, *, by_chat_id: bool = True) -> None:
    try:
        token = await lark_client.get_tenant_access_token(http)  # type: ignore[attr-defined]
    except Exception as e:
        logger.error("無法發送訊息，取得 token 失敗：%s", e)
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    params = {"receive_id_type": "chat_id" if by_chat_id else "open_id"}
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        resp = await http.post(_LARK_SEND_MESSAGE_URL, headers=headers, params=params, json=body, timeout=20)
        if resp.status_code >= 400:
            try:
                errtxt = (await resp.aread()).decode(errors="ignore")
            except Exception:
                errtxt = "<body read error>"
            logger.error("Lark send_text 失敗 %s: %s", resp.status_code, errtxt)
    except Exception as e:
        logger.exception("Lark send_text 發送異常：%s", e)


async def _shim_reply_text(http: httpx.AsyncClient, chat_id: str, text: str, by_chat_id: bool = True) -> None:
    await lark_client.send_text(http, chat_id, text, by_chat_id=by_chat_id)  # type: ignore[attr-defined]


# ---- 動態注入（只有缺少時才補上；不覆蓋你原有實作） ----
if not hasattr(lark_client, "get_tenant_access_token"):
    setattr(lark_client, "get_tenant_access_token", _shim_get_tenant_access_token)
    logger.info("[compat] app.lark_client.get_tenant_access_token 注入完成")

if not hasattr(lark_client, "send_text"):
    setattr(lark_client, "send_text", _shim_send_text)
    logger.info("[compat] app.lark_client.send_text 注入完成")

if not hasattr(lark_client, "reply_text"):
    setattr(lark_client, "reply_text", _shim_reply_text)
    logger.info("[compat] app.lark_client.reply_text 注入完成")

# 其他模組 from app import lark_client 時，會拿到已被補強的模組
__all__ = ["lark_client", "config", "settings"]
