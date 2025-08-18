from __future__ import annotations
from io import BytesIO
from typing import Tuple
import PyPDF2

def pdf_bytes_to_text(data: bytes, *, max_pages: int = 30) -> Tuple[str, int]:
    """回傳 (文字, 實際處理的頁數)。"""
    reader = PyPDF2.PdfReader(BytesIO(data))
    pages = min(len(reader.pages), max_pages)
    chunks = []
    for i in range(pages):
        try:
            chunks.append(reader.pages[i].extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(chunks).strip(), pages
