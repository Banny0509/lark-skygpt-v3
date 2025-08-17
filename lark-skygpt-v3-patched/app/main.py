# app/main.py
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, Tuple, List

import httpx
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import init_db, AsyncSessionFactory
from . import crud, lark_client, openai_client, utils, tasks

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
logger = logging.getLogger("skygpt-web")

# -----------------------------
# 应用生命周期：启动/关闭资源
# -----------------------------
shared_state: Dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：DB 初始化 + 共享 HTTP 客户端
    await init_db()
    http = httpx.AsyncClient(timeout=30.0)
    shared_state["http"] = http
    logger.info("Web app started. Timezone=%s BOT_NAME=%s", settings.TIMEZONE, settings.BOT_NAME)
    try:
        yield
    finally:
        # 关闭
        try:
            await http.aclose()
        except Exception:
            pass
        logger.info("Web app stopped.")

app = FastAPI(lifespan=lifespan)


# -----------------------------
# 健康检查与根路径
# -----------------------------
@app.get("/")
async def root_ok():
    return PlainTextResponse("ok")

@app.get("/healthz")
async def healthz():
    # 尽量不做外部依赖检查，避免健康检查被依赖阻塞
    return JSONResponse({
        "status": "ok",
        "now": utils.now_local().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "tz": settings.TIMEZONE,
        "openai": bool(settings.OPENAI_API_KEY),
    })


# -----------------------------
# Webhook：接收 Lark 事件
# -----------------------------
@app.post("/webhook/lark")
async def lark_event(request: Request, db: AsyncSession = Depends(lambda: AsyncSessionFactory())):
    """
    Lark 回调入口：
    - challenge 验证：原样返回 {"challenge": "..."}
    - 消息事件：文本/图片/文件 等
    """
    # 1) 读取 JSON Body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"code": 0})

    # 2) challenge 验证（Lark 会先发带 challenge 的请求）
    if isinstance(body, dict) and "challenge" in body:
        return JSONResponse({"challenge": body["challenge"]})

    # 3) 解析事件
    event = body.get("event", {}) or {}
    header = body.get("header", {}) or {}
    etype = header.get("event_type") or event.get("type")
    http: httpx.AsyncClient = shared_state["http"]

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
            "text": content.get("text"),
            "file_key": content.get("file_key"),
            "image_key": content.get("image_key"),
        })
        await db.commit()
    except Exception:
        await db.rollback()

    # 6) 分类型处理
    try:
        # --- 文本 ---
        if msg_type == "text":
            text = (content.get("text") or "").strip()
            if not text:
                return JSONResponse({"code": 0})

            # 群聊：必须 @BOT_NAME 才回（命令/对话）
            if chat_type == "group":
                if not _is_bot_mentioned(msg, settings.BOT_NAME):
                    # 未被 @：不响应（但已存DB）
                    return JSONResponse({"code": 0})
                # 清理 @ 文本（可选）
                text = _strip_bot_mention(text, settings.BOT_NAME)

            # 指令
            if text.startswith("/help"):
                await lark_client.send_text_to_chat(http, chat_id,
                    "指令：\n"
                    "/time 現在時間\n"
                    "/date 今日日期\n"
                    "/summary 立即彙整昨天摘要（只此群）"
                )
                return JSONResponse({"code": 0})

            if text.startswith("/time"):
                await lark_client.send_text_to_chat(
                    http, chat_id, utils.now_local().strftime("現在時間：%Y-%m-%d %H:%M:%S %Z")
                )
                return JSONResponse({"code": 0})

            if text.startswith("/date"):
                await lark_client.send_text_to_chat(
                    http, chat_id, utils.now_local().strftime("今日日期：%Y-%m-%d（%A）")
                )
                return JSONResponse({"code": 0})

            if text.startswith("/summary"):
                # 立即只对当前群做“昨天摘要”
                try:
                    await tasks.summarize_for_single_chat(http, chat_id)
                except Exception as e:
                    logger.exception("summary failed: %s", e)
                    await lark_client.send_text_to_chat(http, chat_id, "摘要失敗，請稍後再試。")
                return JSONResponse({"code": 0})

            # 走对话：优先 OpenAI，有 key；否则降级回传文本片段
            reply = await openai_client.reply_text_or_fallback(http, text)
            await lark_client.send_text_to_chat(http, chat_id, reply)
            return JSONResponse({"code": 0})

        # --- 图片 ---
        if msg_type == "image":
            image_key = content.get("image_key")
            if not image_key:
                return JSONResponse({"code": 0})
            # 下载图片 → 视觉描述
            desc = await openai_client.describe_image_or_fallback(http, image_key)
            await lark_client.send_text_to_chat(http, chat_id, desc)
            return JSONResponse({"code": 0})

        # --- 文件 ---
        if msg_type == "file":
            file_key = content.get("file_key")
            file_name = content.get("file_name") or ""
            if not file_key:
                return JSONResponse({"code": 0})
            # 下载并抽取文本（支持 PDF/Word/Excel/CSV/TXT）
            text = await lark_client.download_and_extract_text(http, file_key, file_name)
            # 再交给 OpenAI 摘要（或降级）
            summary = await openai_client.summarize_text_or_fallback(http, text)
            await lark_client.send_text_to_chat(http, chat_id, summary)
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
    chat_type = msg.get("chat_type")  # 'p2p' or 'group'
    sender = msg.get("sender") or {}
    sender_open_id = (sender.get("sender_id") or {}).get("open_id") or ""
    # create_time 为毫秒字符串
    try:
        create_ms = int(msg.get("create_time") or "0")
    except Exception:
        create_ms = 0

    # content 是 JSON 字符串
    content_str = msg.get("content") or "{}"
    try:
        content = json.loads(content_str)
    except Exception:
        content = {}

    # 文件/图片的资源 key 也挂在 content
    if msg_type == "image":
        image_key = (msg.get("image_key") or
                     (content.get("image_key") if isinstance(content, dict) else None))
        content = {"image_key": image_key}
    elif msg_type == "file":
        file_key = msg.get("file_key") or (content.get("file_key") if isinstance(content, dict) else None)
        file_name = msg.get("file_name") or (content.get("file_name") if isinstance(content, dict) else None)
        content = {"file_key": file_key, "file_name": file_name}

    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "msg_type": msg_type,
        "chat_type": chat_type,
        "sender_open_id": sender_open_id,
        "create_ms": create_ms,
        "content": content if isinstance(content, dict) else {},
    }


def _normalize_name(name: str) -> str:
    """标准化名称做宽松匹配：去掉@与空格，小写化"""
    if not name:
        return ""
    return name.replace("@", "").replace(" ", "").strip().casefold()


def _is_bot_mentioned(msg: Dict[str, Any], bot_name: str) -> bool:
    """
    群聊是否 @ 了机器人：
    - 先对比 mentions 里的 name（宽松大小写/空格/有无@）
    - 如果后续你要改成按 open_id/key 来匹配，可在这里增强
    """
    mentions: List[Dict[str, Any]] = msg.get("mentions") or []
    if not mentions:
        return False

    want = _normalize_name(bot_name)
    for m in mentions:
        nm = _normalize_name(m.get("name") or "")
        if nm and nm == want:
            return True
        # 兼容某些客户端把文本里带 @Name 的情况
        key = m.get("key") or ""
        if key.startswith("@") and _normalize_name(key) == want:
            return True
    return False


def _strip_bot_mention(text: str, bot_name: str) -> str:
    """从文本中移除前导的 @BOT_NAME，避免影响命令识别"""
    if not text:
        return text
    t = text.lstrip()
    norm = _normalize_name(bot_name)
    # 常见格式：'@Name 內容' 或 '<at user_id=xxx>@Name</at> 內容'
    if t.startswith("@"):
        # 去掉第一个词（@Name），再 strip 一次
        parts = t.split(" ", 1)
        if parts:
            # 宽松判断第一个词是否就是 @Bot
            leading = _normalize_name(parts[0])
            if leading.startswith(norm):
                return parts[1] if len(parts) > 1 else ""
    return text
