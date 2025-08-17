import base64
from typing import Optional
import httpx
from openai import OpenAI
from .config import settings

def _client() -> Optional[OpenAI]:
    if not settings.OPENAI_API_KEY:
        return None
    return OpenAI(api_key=settings.OPENAI_API_KEY)

async def text_completion(prompt: str, system: str = "You are a helpful assistant in Traditional Chinese.") -> str:
    cli = _client()
    if not cli:
        # fallback: return prompt slice
        return prompt[:12000]
    try:
        resp = cli.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system},{"role":"user","content":prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return prompt[:12000]

async def vision_describe(image_bytes: bytes, extra_prompt: str = "請描述圖片重點（繁體中文）。") -> str:
    cli = _client()
    if not cli:
        return "[已接收圖片]"
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        messages = [
            {"role":"system","content":"You are an expert image analyst."},
            {"role":"user","content":[
                {"type":"text","text":extra_prompt},
                {"type":"image_url","image_url":{"url":f"data:image/png;base64,{b64}"}}
            ]}
        ]
        resp = cli.chat.completions.create(model="gpt-4o-mini", messages=messages, temperature=0.2)
        return resp.choices[0].message.content.strip()
    except Exception:
        return "[圖片解析失敗]"
