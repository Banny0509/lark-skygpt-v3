
# app/utils.py
import io
import base64
from typing import Tuple, Optional

from PIL import Image
from pdfminer.high_level import extract_text as pdf_extract_text

def detect_mime(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        return "image/jpeg"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.endswith(".csv"):
        return "text/csv"
    if name.endswith(".txt") or name.endswith(".md"):
        return "text/plain"
    if name.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/octet-stream"

def pdf_to_text(data: bytes, max_chars: int = 200_000) -> str:
    try:
        text = pdf_extract_text(io.BytesIO(data)) or ""
        return text[:max_chars]
    except Exception as e:
        return f"[PDF 文字提取失败: {e!s}]"

def excel_to_text(data: bytes, max_rows: int = 50) -> str:
    try:
        import pandas as pd
        import io as _io
        df = pd.read_excel(_io.BytesIO(data))
        df = df.head(max_rows)
        return df.to_markdown(index=False)
    except Exception as e:
        return f"[Excel 读取失败: {e!s}]"

def image_bytes_to_data_url(data: bytes, mime: str) -> str:
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def compress_image(data: bytes, max_side: int = 1600) -> Tuple[bytes, str]:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w*scale), int(h*scale)))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, "application/octet-stream"
