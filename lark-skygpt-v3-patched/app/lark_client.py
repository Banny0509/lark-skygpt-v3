# app/lark_client.py
import logging
import asyncio
from typing import Dict, Any, Tuple, Optional
import httpx
from redis.asyncio import Redis

from .config import settings
from . import utils

# 使用與 web/worker 一致的日誌記錄器
logger = logging.getLogger("web") or logging.getLogger("worker")

# --- Redis 客戶端，用於快取 tenant_access_token ---
redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
TOKEN_CACHE_KEY = f"lark_tenant_access_token:{settings.APP_ID}"
TOKEN_EXPIRATION_SEC = 7000  # Lark token 效期為 7200 秒，我們提早更新

# --- 非同步鎖，防止多個程序同時刷新 token ---
token_lock = asyncio.Lock()

async def get_tenant_access_token(http_client: httpx.AsyncClient) -> str:
    """
    取得應用程式的 tenant_access_token。
    優先從 Redis 快取讀取，若不存在，則向 Lark API 請求並存入快取。
    """
    cached_token = await redis_client.get(TOKEN_CACHE_KEY)
    if cached_token:
        return cached_token

    async with token_lock:
        cached_token = await redis_client.get(TOKEN_CACHE_KEY)
        if cached_token:
            return cached_token

        logger.info("快取中無 token，正在向 Lark API 請求新的 tenant_access_token...")
        url = f"{settings.LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": settings.APP_ID, "app_secret": settings.APP_SECRET}
        
        try:
            response = await http_client.post(url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if data.get("code") == 0:
                token = data["tenant_access_token"]
                await redis_client.set(TOKEN_CACHE_KEY, token, ex=TOKEN_EXPIRATION_SEC)
                logger.info("成功取得並快取了新的 token。")
                return token
            else:
                err_msg = f"Lark API 獲取 token 失敗: {data.get('msg', '未知錯誤')}"
                logger.error(err_msg)
                raise ConnectionError(err_msg)
        except Exception as e:
            logger.exception(f"獲取 tenant_access_token 失敗: {e}")
            raise

async def _api_request(http_client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """
    統一的 API 請求函式，自動處理 token 和認證標頭。
    """
    token = await get_tenant_access_token(http_client)
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    
    full_url = url if url.startswith("http") else f"{settings.LARK_BASE}{url}"
    
    response = await http_client.request(method, full_url, headers=headers, **kwargs)
    response.raise_for_status()
    return response

async def reply_message(http_client: httpx.AsyncClient, message_id: str, text: str):
    """使用 message_id 回覆一則訊息。"""
    if not text:
        logger.warning("嘗試發送空訊息，已忽略。")
        return

    try:
        url = f"/open-apis/im/v1/messages/{message_id}/reply"
        payload = {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
        response = await _api_request(http_client, "POST", url, json=payload)
        data = response.json()
        if data.get("code") != 0:
            log_id = response.headers.get("X-Tt-Logid")
            logger.error(f"Lark API 回覆訊息 {message_id} 失敗: {data.get('msg')} (log_id: {log_id})")
    except Exception as e:
        logger.exception(f"回覆訊息 {message_id} 失敗: {e}")

async def send_message(http_client: httpx.AsyncClient, chat_id: str, text: str):
    """主動發送訊息到指定的 chat_id。"""
    if not text:
        logger.warning(f"嘗試發送空訊息到 chat_id {chat_id}，已忽略。")
        return

    try:
        url = "/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        response = await _api_request(http_client, "POST", url, json=payload)
        data = response.json()
        if data.get("code") != 0:
            log_id = response.headers.get("X-Tt-Logid")
            logger.error(f"Lark API 發送訊息至 {chat_id} 失敗: {data.get('msg')} (log_id: {log_id})")
    except Exception as e:
        logger.exception(f"發送訊息至 {chat_id} 失敗: {e}")

async def get_message_resource(
    http_client: httpx.AsyncClient, message_id: str, file_key: str, resource_type: str
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    下載訊息中的資源，例如圖片或檔案。
    resource_type: "image" 或 "file"
    """
    try:
        url = f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}"
        response = await _api_request(http_client, "GET", url, follow_redirects=True)
        
        content_type = response.headers.get("Content-Type")
        header_disp = response.headers.get("Content-Disposition")
        
        filename = None
        if header_disp:
            # 從 Content-Disposition 標頭解析檔名
            parts = [p.strip() for p in header_disp.split(';')]
            for part in parts:
                if part.lower().startswith("filename="):
                    filename = part[len("filename="):].strip('"')
                    break
        
        # 如果無法從標頭解析，則根據類型猜測
        if not filename:
            filename = utils.guess_filename(file_key, content_type, None)

        return response.content, filename, content_type
    except Exception as e:
        logger.exception(f"下載資源 {file_key} 失敗: {e}")
        return None, None, None

