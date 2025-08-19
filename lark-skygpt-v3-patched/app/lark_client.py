# app/lark_client.py
import json
import logging
import httpx

from .config import settings

logger = logging.getLogger(__name__)

LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_SEND_MESSAGE_URL = "https://open.larksuite.com/open-apis/im/v1/messages"


async def get_tenant_access_token(http: httpx.AsyncClient) -> str:
    """
    與 scheduler / tasks 相容的 API：
    以應用身分取得 Lark tenant_access_token。
    """
    if not (settings.LARK_APP_ID and settings.LARK_APP_SECRET):
        raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET 未配置，無法取得 tenant_access_token")

    resp = await http.post(
        LARK_TENANT_TOKEN_URL,
        json={"app_id": settings.LARK_APP_ID, "app_secret": settings.LARK_APP_SECRET},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"取得 tenant_access_token 失敗：{data}")
    return token


async def send_text(http: httpx.AsyncClient, chat_id: str, text: str, *, by_chat_id: bool = True) -> None:
    """
    通用：在群組/單聊回覆純文字訊息。
    保持輕量，若發送失敗僅記錄錯誤，不拋出以免影響主流程/排程。
    """
    token = await get_tenant_access_token(http)
    headers = {"Authorization": f"Bearer {token}"}
    params = {"receive_id_type": "chat_id" if by_chat_id else "open_id"}
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    resp = await http.post(LARK_SEND_MESSAGE_URL, headers=headers, params=params, json=body, timeout=20)
    if resp.status_code >= 400:
        try:
            errtxt = (await resp.aread()).decode(errors="ignore")
        except Exception:
            errtxt = "<body read error>"
        logger.error("Lark send_text 失敗 %s: %s", resp.status_code, errtxt)
