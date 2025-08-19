
# app/lark_client.py
import time
import httpx
from typing import Optional, Tuple

from .config import settings
from .utils import detect_mime, pdf_to_text, excel_to_text, compress_image
from .openai_client import vision_prompt, summarize

TOKEN_CACHE: Tuple[float, str] = (0.0, "")  # (expires_at, token)

async def get_tenant_access_token(http: httpx.AsyncClient) -> str:
    global TOKEN_CACHE
    now = time.time()
    if TOKEN_CACHE[1] and TOKEN_CACHE[0] - now > 60:
        return TOKEN_CACHE[1]

    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": settings.LARK_APP_ID, "app_secret": settings.LARK_APP_SECRET}
    r = await http.post(url, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    token = data.get("tenant_access_token", "")
    expires_in = data.get("expire", 3600)
    TOKEN_CACHE = (now + expires_in, token)
    return token

async def reply_text(http: httpx.AsyncClient, receive_id: str, text: str, by_chat_id: bool = False) -> None:
    token = await get_tenant_access_token(http)
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "receive_id_type": "chat_id" if by_chat_id else "open_id",
        "content": {"text": text},
        "msg_type": "text",
        "receive_id": receive_id,
    }
    body["content"] = httpx.dumps(body["content"])
    r = await http.post(url, headers=headers, params={"receive_id_type": body["receive_id_type"]}, json=body, timeout=20)
    try:
        r.raise_for_status()
    except Exception:
        pass

async def download_file(http: httpx.AsyncClient, file_key: str) -> Tuple[str, bytes]:
    token = await get_tenant_access_token(http)
    url = f"https://open.larksuite.com/open-apis/im/v1/files/{file_key}/download"
    headers = {"Authorization": f"Bearer {token}"}
    r = await http.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    fname = r.headers.get("Content-Disposition", "file").split("filename=")[-1].strip('"')
    return fname, r.content

async def download_and_extract_text(http: httpx.AsyncClient, file_key: str, filename_hint: Optional[str] = None) -> str:
    fname, data = await download_file(http, file_key)
    if not fname and filename_hint:
        fname = filename_hint
    mime = detect_mime(fname or "")
    if mime == "application/pdf":
        return pdf_to_text(data)
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return excel_to_text(data)
    if mime.startswith("image/"):
        img_bytes, img_mime = compress_image(data)
        return vision_prompt(img_bytes, img_mime, prompt="请阅读图片并提取文字与重要信息，最后给出重点摘要。")
    if mime in ("text/plain", "text/csv"):
        try:
            return data.decode("utf-8", errors="ignore")[:200000]
        except Exception:
            return "[文字文件解码失败]"
    return pdf_to_text(data)
