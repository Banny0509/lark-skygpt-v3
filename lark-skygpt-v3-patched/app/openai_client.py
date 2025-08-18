# app/openai_client.py
import os
import json
import logging
from typing import Optional

import httpx
from .config import settings

logger = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


async def _chat_completion(http: httpx.AsyncClient, system_prompt: str, user_prompt: str,
                           model: str = DEFAULT_MODEL,
                           temperature: float = 0.7,
                           max_tokens: int = 800) -> str:
    """
    基礎 Chat Completions 呼叫（文字專用）。需 settings.OPENAI_API_KEY。
    """
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
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.error("Unexpected OpenAI response: %s", json.dumps(data)[:1000])
        raise


# ---------------------------
# Public helpers
# ---------------------------

async def reply_text_or_fallback(http: httpx.AsyncClient, text: str) -> str:
    """
    一般聊天：有金鑰 → 走 OpenAI；無金鑰 → 安全降級。
    """
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        # 降級：回傳截斷版本，避免沉默
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
        # 再次降級，避免整體失敗
        snippet = (text or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "..."
        return f"(降級回覆) 目前無法連線到模型，先回覆你的原話片段：{snippet}"


async def summarize_text_or_fallback(http: httpx.AsyncClient, text: str) -> str:
    """
    摘要：有金鑰 → 走 OpenAI；無金鑰 → 給出簡短條列降級版。
    """
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        # 降級摘要：簡單壓縮到要點
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        lines = lines[:10]  # 粗略取前幾行
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


async def describe_image_or_fallback(http: httpx.AsyncClient, image_key: str) -> str:
    """
    圖片描述：目前預設穩定降級（若你已有圖片可公開訪問的 URL，我可幫你接 Vision）。
    """
    # 若你已在 lark_client 實作取得圖片 URL，可把 URL 傳進來，然後改走 Vision：
    #   1) 將 messages 改為帶 image_url 的多模態內容
    #   2) 模型可用 gpt-4o-mini / gpt-4o
    #
    # 先提供穩定降級，以免阻塞流程
    return f"我收到了圖片（image_key={image_key}）。目前未啟用圖像解析，如需圖像理解請提供文字描述，或告知我可存取的圖片 URL。"
