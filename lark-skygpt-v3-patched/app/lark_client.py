# app/lark_client.py
import json
from typing import List, Dict, Any, Tuple, Optional

import httpx
import redis.asyncio as redis

from .config import settings

# Redis：用于缓存 tenant_access_token
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)


# -----------------------------
# 鉴权：获取 tenant_access_token
# -----------------------------
async def get_tenant_access_token(http: httpx.AsyncClient) -> str:
    token = await _redis.get("tenant_access_token")
    if token:
        return token

    url = f"{settings.LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal"
    r = await http.post(url, json={"app_id": settings.APP_ID, "app_secret": settings.APP_SECRET}, timeout=20.0)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise httpx.HTTPStatusError(f"get tenant access token failed: {data}", request=r.request, response=r)
    token = data.get("tenant_access_token")
    # 默认有效期 2 小时，缓存 90 分钟
    if token:
        await _redis.setex("tenant_access_token", 90 * 60, token)
        return token
    raise httpx.HTTPStatusError("tenant_access_token missing", request=r.request, response=r)


# -----------------------------
# 发送文本到 chat（群聊/私聊）
# -----------------------------
async def send_text_to_chat(http: httpx.AsyncClient, chat_id: str, text: str):
    tat = await get_tenant_access_token(http)
    url = f"{settings.LARK_BASE}/open-apis/im/v1/messages"
    headers = {
        "Authorization": f"Bearer {tat}",
        "Content-Type": "application/json",
    }
    params = {"receive_id_type": "chat_id"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False)
    }
    r = await http.post(url, headers=headers, params=params, json=payload, timeout=20.0)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        # 统一抛错交给上层日志
        raise httpx.HTTPStatusError(f"send_text_to_chat failed: {data}", request=r.request, response=r)


# -----------------------------
# 拉取“消息中的资源文件”
# 必须携带：message_id + key + type
# type: "image" | "file"
# 返回: (二进制数据, 文件名, Content-Type)
# -----------------------------
async def get_message_resource(
    http: httpx.AsyncClient,
    message_id: str,
    key: str,
    res_type: str
) -> Tuple[bytes, Optional[str], Optional[str]]:
    tat = await get_tenant_access_token(http)
    url = f"{settings.LARK_BASE}/open-apis/im/v1/messages/{message_id}/resources/{key}"
    headers = {"Authorization": f"Bearer {tat}"}
    params = {"type": res_type}
    r = await http.get(url, headers=headers, params=params, timeout=60.0)
    r.raise_for_status()

    ct = r.headers.get("Content-Type")
    disp = r.headers.get("Content-Disposition", "") or ""
    name = None
    if "filename=" in disp:
        try:
            name = disp.split("filename=", 1)[1].strip('"; ')
        except Exception:
            name = None
    return r.content, name, ct
