
# app/openai_client.py
import base64
from typing import List, Dict, Any, Optional

from .config import settings

try:
    from openai import OpenAI
    _client = OpenAI(api_key=settings.OPENAI_API_KEY, base_url=(settings.OPENAI_BASE_URL or None))
except Exception:
    _client = None

NO_LLM_MSG = "系统尚未配置 OPENAI_API_KEY，请在环境变量中加入 OPENAI_API_KEY 后重试。"

def _ensure() -> bool:
    return _client is not None and bool(settings.OPENAI_API_KEY)

def chat(messages: List[Dict[str, Any]], model: Optional[str] = None, max_tokens: Optional[int] = None) -> str:
    if not _ensure():
        return NO_LLM_MSG
    model = model or settings.OPENAI_CHAT_MODEL
    resp = _client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=max_tokens or settings.MAX_REPLY_TOKENS,
    )
    return (resp.choices[0].message.content or "").strip()

def summarize(text: str, system_prompt: Optional[str] = None, model: Optional[str] = None) -> str:
    if not _ensure():
        return NO_LLM_MSG
    model = model or settings.OPENAI_SUMMARY_MODEL
    sys_msg = system_prompt or "你是严谨而专业的中文摘要助手，请将输入内容要点化为条列，必要时加上小标题。"
    messages = [
        { "role": "system", "content": sys_msg },
        { "role": "user", "content": text[:120000] }
    ]
    return chat(messages, model=model, max_tokens=1200)

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")

def vision_prompt(file_bytes: bytes, media_type: str, prompt: str = "请阅读此内容并用中文摘要重点；若是表格，保留表头关键栏位。") -> str:
    if not _ensure():
        return NO_LLM_MSG

    b64 = _b64(file_bytes)
    data_url = f"data:{media_type};base64,{b64}"

    messages = [
        {"role": "system", "content": "你是文件与图片理解助手，请用简体中文回复。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    return chat(messages, model=settings.OPENAI_VISION_MODEL, max_tokens=900)
