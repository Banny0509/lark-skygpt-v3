# app/openai_client.py
import os
import json
import logging
import base64
from typing import Optional

import httpx
from .config import settings

logger = logging.getLogger(__name__)

# ---- OpenAI Chat Completions 端點與模型 ----
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# =========================
# 核心：文字 Chat Completions
# =========================
async def _chat_completion(
    http: httpx.AsyncClient,
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.7,
    max_tokens: int = 800,
) -> str:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    resp = await http.post(OPENAI_API_URL, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 400:
        txt = (await resp.aread()).decode(errors="ignore")
        logger.error("OpenAI API error %s: %s", resp.status_code, txt)
        raise RuntimeError(f"OpenAI API error {resp.status_code}")

    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

# =========================
# 公開：文字聊天 / 摘要（保留你原有行為與語氣）
# =========================
async def reply_text_or_fallback(http: httpx.AsyncClient, text: str) -> str:
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        snippet = (text or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "..."
        return f"(降級回覆) 你說：{snippet}"

    sys_prompt = (
        "你是穩健的中文 AI 助理，回覆需：\n"
        "1) 精準、條列化、避免長篇廢話\n"
        "2) 若被要求翻譯/總結，遵循語言與篇幅\n"
        "3) 無法確定時明確詢問但給出可能方向"
    )
    try:
        return await _chat_completion(http, sys_prompt, text)
    except Exception as e:
        logger.exception("reply_text_or_fallback failed: %s", e)
        snippet = (text or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "..."
        return f"(降級回覆) 目前無法連線到模型，先回覆你的原話片段：{snippet}"

async def summarize_text_or_fallback(http: httpx.AsyncClient, text: str) -> str:
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        lines = lines[:10]
        bullets = "\n".join(f"- {ln[:120]}" for ln in lines)
        return f"(降級摘要)\n{bullets}" if bullets else "(降級摘要) 無可摘要內容"

    sys_prompt = (
        "你是嚴謹的中文摘要助手，輸出需：\n"
        "• 保持關鍵事實與數字\n"
        "• 使用條列式，避免冗長\n"
        "• 若原文含任務/決策/未決，請條列標註"
    )
    try:
        return await _chat_completion(http, sys_prompt, text, temperature=0.4, max_tokens=900)
    except Exception as e:
        logger.exception("summarize_text_or_fallback failed: %s", e)
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        lines = lines[:10]
        bullets = "\n".join(f"- {ln[:120]}" for ln in lines)
        return f"(降級摘要)\n{bullets}" if bullets else "(降級摘要) 無可摘要內容"

# =========================
# Lark：租戶令牌 & 「訊息資源」下載（符合官方規範）
# =========================
LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
# 從訊息中取資源必須帶 message_id 與 file_key，並指定 type=image|file
LARK_MSG_RESOURCE_TPL = "https://open.larksuite.com/open-apis/im/v1/messages/{message_id}/resources/{file_key}"

async def _get_tenant_access_token(http: httpx.AsyncClient) -> str:
    if not (settings.LARK_APP_ID and settings.LARK_APP_SECRET):
        raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET 未配置，無法下載圖片/檔案")
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

async def _download_message_resource(
    http: httpx.AsyncClient,
    message_id: str,
    file_key: str,
    rtype: str,  # "image" 或 "file"
) -> bytes:
    """
    依 Lark 官方規範：從訊息中下載資源（二進位），支援 image / file。
    需匹配的條件：
      - 應用擁有權限（im:files:read / im:message / im:message:send）
      - 應用已加入該訊息所在的會話
      - message_id 與 file_key 對應同一則訊息
    """
    assert rtype in ("image", "file"), "rtype 需為 image 或 file"
    token = await _get_tenant_access_token(http)
    url = LARK_MSG_RESOURCE_TPL.format(message_id=message_id, file_key=file_key)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    r = await http.get(url, headers=headers, params={"type": rtype}, timeout=120)
    # 常見錯誤碼對應（以官方文件為準）
    if r.status_code == 400:
        # 234003: File not in message / 234004: App not in chat / 234037: 超上限
        logger.error("Lark 400: %s", await r.aread())
    if r.status_code == 401:
        logger.error("Lark 401 Unauthorized: %s", await r.aread())
    if r.status_code == 403:
        logger.error("Lark 403 Forbidden (可能外部群或受限模式): %s", await r.aread())
    r.raise_for_status()
    return r.content

# =========================
# 圖片 → Vision（多模態）：使用「訊息資源」端點
# =========================
async def describe_image_from_message_or_fallback(
    http: httpx.AsyncClient, message_id: str, image_key: str
) -> str:
    """
    從訊息下載圖片（message_id + image_key + type=image）→ OpenAI Vision。
    """
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        return f"(降級) 收到圖片 image_key={image_key}，但未配置 OPENAI_API_KEY。"

    try:
        img_bytes = await _download_message_resource(http, message_id, image_key, rtype="image")
    except httpx.HTTPStatusError as e:
        return f"(降級) 圖像下載失敗（{e.response.status_code}）：請確認應用權限與是否已加入該群，以及 message_id 與 file_key 是否匹配"
    except Exception as e:
        logger.exception("下載圖片失敗：%s", e)
        # 這裡是你日志中曾出現的錯誤行，已修正為 {image_key}（不要有多餘的 )）
        return f"(降級) 無法下載圖片（image_key={image_key}）：{e}"

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEFAULT_MODEL,
        "temperature": 0.3,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": "你是中文圖像理解助手，請用繁體中文、條列式給出要點摘要；若含表格，概括關鍵欄位。"},
            {"role": "user", "content": [
                {"type": "text", "text": "請閱讀這張圖片，擷取主要資訊與能辨識的文字，最後條列重點結論。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }

    try:
        resp = await http.post(OPENAI_API_URL, headers=headers, json=payload, timeout=90)
        if resp.status_code >= 400:
            txt = (await resp.aread()).decode(errors="ignore")
            logger.error("OpenAI Vision error %s: %s", resp.status_code, txt)
            return f"(降級) 圖像理解調用失敗（{resp.status_code}）：{txt[:200]}"
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.exception("Vision 調用異常：%s", e)
        return f"(降級) 圖像理解暫不可用：{e}"

# =========================
# PDF → 圖片（首頁）→ Vision（多模態）：使用「訊息資源：type=file」
# =========================
async def describe_pdf_from_message_or_fallback(
    http: httpx.AsyncClient, message_id: str, file_key: str
) -> str:
    """
    從訊息下載 PDF（message_id + file_key + type=file）→ 轉圖（首頁）→ OpenAI Vision。
    """
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        return f"(降級) 收到 PDF file_key={file_key}，但未配置 OPENAI_API_KEY。"

    # 下載 PDF 二進位
    try:
        pdf_bytes = await _download_message_resource(http, message_id, file_key, rtype="file")
    except httpx.HTTPStatusError as e:
        return f"(降級) 下載 PDF 失敗（{e.response.status_code}）：請確認應用權限、是否已加入該群，以及 message_id 與 file_key 是否匹配"
    except Exception as e:
        logger.exception("下載 PDF 失敗：%s", e)
        return f"(降級) 下載 PDF 錯誤：{e}"

    # 轉圖（PyMuPDF）
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count == 0:
            return "(降級) PDF 內容為空，無法解析。"
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 放大解析度，提高清晰度
        img_bytes = pix.tobytes("png")
    except Exception as e:
        logger.exception("PDF 轉圖失敗：%s", e)
        return f"(降級) PDF 轉圖失敗，請改傳圖片或檢查權限：{e}"

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:image/png;base64,{b64}"

    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEFAULT_MODEL,
        "temperature": 0.3,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": "你是中文文件理解助手。對 PDF（已轉為圖片）請用繁體中文條列：主要資訊、可辨識文字、表格重點與結論。"},
            {"role": "user", "content": [
                {"type": "text", "text": "請閱讀這份 PDF（已轉成圖片的首頁），擷取關鍵文字與結論，條列重點。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }

    try:
        resp = await http.post(OPENAI_API_URL, headers=headers, json=payload, timeout=120)
        if resp.status_code >= 400:
            txt = (await resp.aread()).decode(errors="ignore")
            logger.error("OpenAI Vision (PDF) error %s: %s", resp.status_code, txt)
            return f"(降級) PDF 圖像理解調用失敗（{resp.status_code}）：{txt[:200]}"
    except Exception as e:
        logger.exception("Vision (PDF) 調用異常：%s", e)
        return f"(降級) PDF 圖像理解暫不可用：{e}"

    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()
