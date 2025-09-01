# app/lark_client.py
import json
import os
import logging
import httpx

LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_SEND_MESSAGE_URL = "https://open.larksuite.com/open-apis/im/v1/messages"

logger = logging.getLogger("lark_fallback")

def _resolve_lark_credentials():
    app_id = (
        os.getenv("LARK_APP_ID") or os.getenv("FEISHU_APP_ID") or os.getenv("APP_ID")
    )
    app_secret = (
        os.getenv("LARK_APP_SECRET") or os.getenv("FEISHU_APP_SECRET") or os.getenv("APP_SECRET")
    )
    if not app_id or not app_secret:
        raise RuntimeError("缺少 Lark 憑證：請設定 LARK_APP_ID/LARK_APP_SECRET")
    return app_id, app_secret

async def _get_tenant_access_token(http: httpx.AsyncClient) -> str:
    app_id, app_secret = _resolve_lark_credentials()
    r = await http.post(LARK_TENANT_TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret}, timeout=20)
    r.raise_for_status()
    tok = r.json().get("tenant_access_token")
    if not tok:
        raise RuntimeError("取得 tenant_access_token 失敗")
    return tok

async def send_text_to_chat(http: httpx.AsyncClient, chat_id: str, text: str) -> None:
    token = await _get_tenant_access_token(http)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    params = {"receive_id_type": "chat_id"}
    body = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    resp = await http.post(LARK_SEND_MESSAGE_URL, headers=headers, params=params, json=body, timeout=20)
    if resp.status_code >= 400:
        errtxt = (await resp.aread()).decode(errors="ignore")
        raise RuntimeError(f"send_text_to_chat 失敗 {resp.status_code}: {errtxt}")
    logger.info("lark_client.send_text_to_chat OK chat_id=%s", chat_id)
