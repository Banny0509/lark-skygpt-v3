from __future__ import annotations
import logging
from typing import Optional
from openai import OpenAI
from .config import settings

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None

def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client

def summarize_text(text: str, *, filename: str = "document.pdf") -> str:
    """用小模型快速做摘要；長文自動截斷到 ~10k 字，避免 token 爆掉。"""
    text = (text or "").strip()
    if len(text) > 10000:
        text = text[:10000] + "\n\n[... 內容過長，已截斷 ...]"
    prompt = (
        f"你是一位中文助理，請用條列重點摘要這份檔案《{filename}》。"
        "必須包含：1) 檔案目的、2) 重要數字/日期、3) 需要決策或行動項、4) 風險與注意事項。"
    )
    resp = client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt + "\n\n--- 原文開始 ---\n" + text},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()
