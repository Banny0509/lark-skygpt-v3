import json
import httpx
import redis.asyncio as redis
from typing import List, Dict, Any, Tuple, Optional
from .config import settings

# Redis token cache
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

async def get_tenant_access_token(http: httpx.AsyncClient) -> str:
    token = await _redis.get("tenant_access_token")
    if token:
        return token
    url = f"{settings.LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal"
    r = await http.post(url, json={"app_id": settings.APP_ID, "app_secret": settings.APP_SECRET}, timeout=20.0)
    r.raise_for_status()
    data = r.json()
    tat = data.get("tenant_access_token")
    expire = int(data.get("expire", 3600))
    await _redis.set("tenant_access_token", tat, ex=max(expire - 60, 60))
    return tat

async def send_text_to_chat(http: httpx.AsyncClient, chat_id: str, text: str) -> None:
    tat = await get_tenant_access_token(http)
    api = f"{settings.LARK_BASE}/open-apis/im/v1/messages?receive_id_type=chat_id"
    payload = {"receive_id": chat_id, "content": json.dumps({"text": text}, ensure_ascii=False), "msg_type": "text"}
    headers = {"Authorization": f"Bearer {tat}"}
    r = await http.post(api, headers=headers, json=payload, timeout=20.0)
    # Best effort; no raise

async def list_chat_messages_between(http: httpx.AsyncClient, chat_id: str, start_ms: int, end_ms: int, page_size: int = 100) -> List[Dict[str, Any]]:
    tat = await get_tenant_access_token(http)
    headers = {"Authorization": f"Bearer {tat}"}
    api = f"{settings.LARK_BASE}/open-apis/im/v1/messages"
    params = {"container_id_type":"chat","container_id":chat_id,"start_time":str(start_ms),"end_time":str(end_ms),"page_size":str(page_size)}
    items: List[Dict[str, Any]] = []
    while True:
        r = await http.get(api, headers=headers, params=params, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        page = data.get("data", {}).get("items", []) or []
        items.extend(page)
        token = data.get("data", {}).get("page_token")
        if not token:
            break
        params["page_token"] = token
    return items

async def download_message_resource(http: httpx.AsyncClient, message_id: str, key: str, res_type: str) -> Tuple[bytes, Optional[str], Optional[str]]:
    # Correct endpoint per Lark docs: messages/{message_id}/resources/{key}?type=file|image
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
            name = disp.split("filename=",1)[1].strip('"; ')
        except Exception:
            name = None
    return r.content, name, ct
