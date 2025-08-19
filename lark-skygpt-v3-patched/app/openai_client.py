# app/openai_client.py
import os
import json
import logging
from typing import Optional
import httpx
from .config import settings
# --- Lark 文件下载（避免 403 的关键） ---
LARK_TENANT_TOKEN_URL = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
LARK_FILE_DOWNLOAD_TPL = "https://open.larksuite.com/open-apis/im/v1/files/{file_key}/download"

async def _get_tenant_access_token(http: httpx.AsyncClient) -> str:
    """获取 Lark 租户访问令牌（内部应用）。"""
    if not (settings.LARK_APP_ID and settings.LARK_APP_SECRET):
        raise RuntimeError("LARK_APP_ID / LARK_APP_SECRET 未配置，无法下载图片")
    r = await http.post(
        LARK_TENANT_TOKEN_URL,
        json={"app_id": settings.LARK_APP_ID, "app_secret": settings.LARK_APP_SECRET},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    tok = data.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"获取 tenant_access_token 失败：{data}")
    return tok

async def _download_lark_image_bytes(http: httpx.AsyncClient, image_key: str) -> bytes:
    """用租户令牌从 Lark 下载图片二进制。"""
    token = await _get_tenant_access_token(http)
    url = LARK_FILE_DOWNLOAD_TPL.format(file_key=image_key)
    r = await http.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if r.status_code == 403:
        # 经典 403：权限未勾 `im:files:read` 或应用未加入群
        raise PermissionError("Lark 403：请确认勾选 im:files:read，并将应用加入目标群后发布生效")
    r.raise_for_status()
    return r.content

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
    图像理解：最小侵入启用 OpenAI Vision。
    - 先用 Lark 官方下载接口拿到图片 bytes（带 tenant_access_token）
    - 转 base64 data URL 作为 image_url
    - 走 Chat Completions 多模态（gpt-4o-mini 默认支持）
    - 若权限/网络异常，返回可读降级提示，不让服务崩溃
    """
    # 无 Key 直接降级（保持你原来的策略）
    if not (settings.OPENAI_API_KEY and settings.OPENAI_API_KEY.strip()):
        return f"(降级) 收到图片 image_key={image_key}，但未配置 OPENAI_API_KEY。"

    try:
        img_bytes = await _download_lark_image_bytes(http, image_key)
    except PermissionError as e:
        # 明确提示 403 场景与该做什么
        return f"(降级) 图像理解权限不足：{e}"
    except Exception as e:
        logger.exception("下载图片失败：%s", e)
        return f"(降级) 无法下载图片（image_key={image_key)）：{e}"

    import base64
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEFAULT_MODEL,  # 默认 gpt-4o-mini；可用 OPENAI_MODEL 覆盖
        "temperature": 0.3,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": "你是中文图像理解助手，请用简体中文、条列方式给出要点摘要；表格请概括关键字段。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请阅读这张图片，提取主要信息与可读文字，并条列重点结论。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }

    try:
        resp = await http.post(OPENAI_API_URL, headers=headers, json=payload, timeout=90)
        if resp.status_code >= 400:
            txt = (await resp.aread()).decode(errors="ignore")
            logger.error("OpenAI Vision error %s: %s", resp.status_code, txt)
            return f"(降级) 图像理解权限不足（{resp.status_code}）：{txt[:200]}"
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.exception("Vision 调用异常：%s", e)
        return f"(降级) 图像理解权限不足：{e}"

