# app/main.py
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, Tuple, List

import httpx
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import init_db, AsyncSessionFactory
from . import crud, lark_client, utils

logger = logging.getLogger("app.main")
logger.setLevel(getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO))

# -----------------------------
# 应用生命周期 & 共享资源
# -----------------------------
shared_state: Dict[str, Any] = {
    "http": None,   # httpx.AsyncClient
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建立共享 HTTP 客户端
    http = httpx.AsyncClient(timeout=30.0)
    shared_state["http"] = http

    # 初始化数据库（自动建表）
    try:
        await init_db()
        logger.info("Database initialized.")
    except Exception:
        logger.exception("init_db failed")

    yield

    # 关闭 HTTP 客户端
    try:
        await http.aclose()
    except Exception:
        pass

app = FastAPI(lifespan=lifespan)


# -----------------------------
# 健康页
# -----------------------------
@app.get("/")
async def root():
    return JSONResponse({
        "ok": True,
        "tz": settings.TIMEZONE,
        "openai": bool(settings.OPENAI_API_KEY),
        "require_mention": bool(getattr(settings, "REQUIRE_MENTION", True)),
    })


# -----------------------------
# Webhook：Lark 回调入口
# -----------------------------
@app.post("/webhook/lark")
async def lark_event(
    request: Request,
    db: AsyncSession = Depends(lambda: AsyncSessionFactory())
):
    """
    Lark 回调入口：
    - challenge 验证：原样返回 {"challenge": "..."}
    - 消息事件：文本/图片/文件 等
    """
    # 1) 读取 Body
    try:
        body = await request.json()
    except Exception:
        logger.warning("invalid json body")
        return JSONResponse({"code": 0})

    # 2) challenge 验证
    if isinstance(body, dict) and "challenge" in body:
        logger.info("Lark challenge received")
        return JSONResponse({"challenge": body["challenge"]})

    # 3) 解析事件
    header = body.get("header", {}) or {}
    event = body.get("event", {}) or {}
    etype = header.get("event_type") or event.get("type")
    logger.info("Event received: etype=%s header_min=%s",
                etype, {k: header.get(k) for k in ("event_id","event_type","create_time")})

    # 只关心 message 相关事件
    if not (etype and "message" in etype):
        return JSONResponse({"code": 0})

    # 4) 基础解析
    msg = event.get("message") or {}
    meta = _parse_msg_basic(msg)
    chat_id = meta["chat_id"]
    message_id = meta["message_id"]
    msg_type = meta["msg_type"]
    chat_type = meta["chat_type"]
    content = meta["content"]
    create_ms = meta["create_ms"]

    # 5) DB 记录（best-effort）
    try:
        await crud.insert_message(db, {
            "chat_id": chat_id,
            "message_id": message_id,
            "ts_ms": create_ms,
            "msg_type": msg_type,
            "chat_type": chat_type,
            "text": content if msg_type == "text" else None,
            "file_key": _safe_key_from_content(content, "file_key") if msg_type == "file" else None,
            "image_key": _safe_key_from_content(content, "image_key") if msg_type == "image" else None,
        })
        await db.commit()
    except Exception:
        logger.exception("insert_message failed")
        try:
            await db.rollback()
        except Exception:
            pass

    # 6) 分类型处理
    http: httpx.AsyncClient = shared_state["http"]

    try:
        # --- 文本 ---
        if msg_type == "text":
            text = (content or "").strip()
            if not text:
                return JSONResponse({"code": 0})

            # 群聊：默认必须 @ 才回（可由 REQUIRE_MENTION 控制）
            if chat_type == "group" and getattr(settings, "REQUIRE_MENTION", True):
                if not _is_bot_mentioned(msg, settings.BOT_NAME):
                    logger.info("Group text ignored (no @). chat_id=%s", chat_id)
                    return JSONResponse({"code": 0})
                # 清理 @
                text = _strip_bot_mention(text, settings.BOT_NAME)

            # 指令
            if text.startswith("/help"):
                await lark_client.send_text_to_chat(
                    http, chat_id,
                    "指令：\n"
                    "/time 现在时间\n"
                    "/date 今日日期\n"
                    "/summary 立即汇整昨天摘要（只此群）"
                )
                return JSONResponse({"code": 0})

            if text.startswith("/time"):
                await lark_client.send_text_to_chat(
                    http, chat_id,
                    utils.now_local().strftime("现在时间：%Y-%m-%d %H:%M:%S %Z")
                )
                return JSONResponse({"code": 0})

            if text.startswith("/date"):
                await lark_client.send_text_to_chat(
                    http, chat_id,
                    utils.now_local().strftime("今日日期：%Y-%m-%d（%A）")
                )
                return JSONResponse({"code": 0})

            if text.startswith("/summary"):
                # 这里保留原有摘要触发逻辑（如有）
                await lark_client.send_text_to_chat(http, chat_id, "已收到摘要指令，稍后完成。")
                return JSONResponse({"code": 0})

            # 其它文本（可接 OpenAI 或自定义逻辑）
            await lark_client.send_text_to_chat(http, chat_id, f"收到：{text}")
            return JSONResponse({"code": 0})

        # --- 图片 ---
        if msg_type == "image":
            cobj = _safe_json(content)
            image_key = (cobj.get("image_key") or "").strip()
            if not image_key:
                logger.warning("image message without image_key. message_id=%s", message_id)
                return JSONResponse({"code": 0})

            try:
                data, fname, ctype = await lark_client.get_message_resource(http, message_id, image_key, "image")
                logger.info("fetched image resource: message_id=%s, image_key=%s, size=%s, name=%s, ctype=%s",
                            message_id, image_key, len(data) if data else 0, fname, ctype)
                # TODO：你的后处理（OCR/存储/转发）
                await lark_client.send_text_to_chat(http, chat_id, f"已收到图片（{fname or '未命名'}）")
            except httpx.HTTPStatusError as e:
                logger.exception("download image failed: %s", e)
            return JSONResponse({"code": 0})

        # --- 文件 ---
        if msg_type == "file":
            cobj = _safe_json(content)
            file_key = (cobj.get("file_key") or "").strip()
            if not file_key:
                logger.warning("file message without file_key. message_id=%s", message_id)
                return JSONResponse({"code": 0})

            try:
                data, fname, ctype = await lark_client.get_message_resource(http, message_id, file_key, "file")
                logger.info("fetched file resource: message_id=%s, file_key=%s, size=%s, name=%s, ctype=%s",
                            message_id, file_key, len(data) if data else 0, fname, ctype)
                # TODO：你的后处理（解析Excel/落盘/存储）
                await lark_client.send_text_to_chat(http, chat_id, f"已收到文件（{fname or '未命名'}）")
            except httpx.HTTPStatusError as e:
                logger.exception("download file failed: %s", e)
            return JSONResponse({"code": 0})

    except Exception as e:
        logger.exception("handle message failed: %s", e)

    return JSONResponse({"code": 0})


# -----------------------------
# 工具函数
# -----------------------------
def _parse_msg_basic(msg: Dict[str, Any]) -> Dict[str, Any]:
    """从 Lark message 提取常用字段"""
    chat_id = msg.get("chat_id")
    message_id = msg.get("message_id")
    msg_type = msg.get("message_type")
    chat_type = msg.get("chat_type")
    content = msg.get("content") or ""
    create_ms = msg.get("create_time") or msg.get("create_time_ms") or 0
    # Lark 把 content 以 JSON 字符串形式给出；文本时 content 为 {"text":"..."}
    if isinstance(content, dict):
        # 兼容异常情况
        try:
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = str(content)
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "msg_type": msg_type,
        "chat_type": chat_type,
        "content": _extract_text_if_text(msg_type, content),
        "create_ms": int(create_ms) if str(create_ms).isdigit() else 0,
    }

def _extract_text_if_text(msg_type: Optional[str], content_str: str) -> str:
    if msg_type != "text":
        return content_str or ""
    try:
        obj = json.loads(content_str or "{}")
        return (obj.get("text") or "").strip()
    except Exception:
        return content_str or ""

def _safe_json(content_str: str) -> Dict[str, Any]:
    try:
        return json.loads(content_str or "{}")
    except Exception:
        return {}

def _safe_key_from_content(content_str: str, key: str) -> Optional[str]:
    try:
        obj = json.loads(content_str or "{}")
        val = (obj.get(key) or "").strip()
        return val or None
    except Exception:
        return None

def _normalize_name(name: str) -> str:
    return (name or "").strip().lower().lstrip("@").replace(" ", "")

def _is_bot_mentioned(msg: Dict[str, Any], bot_name: str) -> bool:
    """
    群聊是否 @ 了机器人：
    - 对比 mentions 里的 name（宽松大小写/空格/有无@）
    """
    mentions: List[Dict[str, Any]] = msg.get("mentions") or []
    if not mentions:
        return False
    want = _normalize_name(bot_name)
    for m in mentions:
        nm = _normalize_name(m.get("name") or "")
        if nm == want:
            return True
    # 兜底：有时文本前缀直接带 @Name
    content = msg.get("content") or ""
    try:
        obj = json.loads(content or "{}")
        t = (obj.get("text") or "").strip()
        if _normalize_name(t).startswith(_normalize_name("@" + bot_name)):
            return True
    except Exception:
        pass
    return False

def _strip_bot_mention(text: str, bot_name: str) -> str:
    """从文本中移除前导的 @BOT_NAME，避免影响命令识别"""
    if not text:
        return text
    t = text.lstrip()
    if t.startswith("@"):
        parts = t.split(" ", 1)
        if len(parts) == 2:
            return parts[1].lstrip()
        return ""
    return t
