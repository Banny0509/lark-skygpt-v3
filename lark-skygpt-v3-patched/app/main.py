# app/main.py
import json
import logging
from typing import Dict, Any, Optional, List

import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks
from openai import AsyncOpenAI

# 匯入您專案中已有的模組
from .config import settings
from .database import init_db, AsyncSessionFactory
from . import crud, lark_client, utils

# --- 日誌與 OpenAI 客戶端初始化 ---
logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format="%(asctime)s %(levelname)s web :: %(message)s",
)
logger = logging.getLogger("web")

aclient: Optional[AsyncOpenAI] = None
if settings.OPENAI_API_KEY:
    try:
        aclient = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        logger.info("OpenAI 客戶端初始化成功。")
    except Exception as e:
        logger.error(f"OpenAI 客戶端初始化失敗: {e}")
else:
    logger.warning("未設定 OPENAI_API_KEY，AI 對話功能將停用。")


# --- FastAPI 應用程式設定 ---
app = FastAPI(
    title="Lark SkyGPT Bot (整合版)",
    description="完整整合指令、檔案處理與 OpenAI AI 對話功能。",
    version="2.0.0"
)

@app.on_event("startup")
async def on_startup():
    """應用程式啟動時，初始化資料庫。"""
    logger.info("Web 服務啟動中...")
    await init_db()
    logger.info("資料庫初始化完成。")


# --- 健康檢查端點 ---
@app.get("/", tags=["服務健康檢查"])
@app.get("/healthz", tags=["服務健康檢查"])
async def health_check():
    """提供健康檢查端點，確認服務狀態與設定。"""
    return {
        "status": "ok",
        "bot_name": settings.BOT_NAME,
        "openai_configured": bool(settings.OPENAI_API_KEY),
    }

# --- Webhook 主要入口 ---
@app.post("/webhook/lark", tags=["Lark Webhook"])
async def handle_lark_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    接收 Lark 事件，並將處理邏輯交給背景任務。
    """
    try:
        payload = await request.json()
    except Exception:
        logger.warning("收到了無效的 JSON 格式請求。")
        return Response(content="無效的 JSON", status_code=400)

    if "challenge" in payload:
        logger.info("回應 Lark 的 URL 驗證請求。")
        return Response(content=json.dumps({"challenge": payload["challenge"]}), media_type="application/json")

    # 將實際的事件處理放到背景執行，避免超時
    background_tasks.add_task(process_event_task, payload)
    
    logger.info("已接收 Lark 事件並交由背景處理。")
    return Response(content="OK", status_code=200)


# ===================================================================
# == 背景任務處理 (整合您的所有邏輯)
# ===================================================================

async def process_event_task(payload: Dict[str, Any]):
    """
    在背景執行的主任務，處理所有來自 Lark 的事件。
    """
    header = payload.get("header", {})
    event = payload.get("event", {})
    event_type = header.get("event_type")

    if event_type != "im.message.receive_v1":
        logger.debug(f"忽略未處理的事件類型: {event_type}")
        return

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        async with AsyncSessionFactory() as db:
            try:
                await handle_message(http_client, db, event)
            except Exception as e:
                logger.exception(f"處理訊息時發生未預期的錯誤: {e}")


async def handle_message(http: httpx.AsyncClient, db: AsyncSession, event: Dict[str, Any]):
    """
    核心訊息處理函式，整合了您的所有功能與 OpenAI。
    """
    msg = event.get("message", {})
    meta = _parse_msg_basic(msg)
    chat_id, message_id, msg_type, chat_type, content, create_ms = meta.values()

    if not all([chat_id, message_id, msg_type]):
        logger.warning(f"收到了不完整的訊息事件: {msg}")
        return

    # 1. 儲存訊息至資料庫 (保留您的邏輯)
    try:
        await crud.insert_message(db, {
            "chat_id": chat_id, "message_id": message_id, "ts_ms": create_ms,
            "msg_type": msg_type, "chat_type": chat_type,
            "text": content if msg_type == "text" else None,
            "file_key": _safe_key_from_content(content, "file_key") if msg_type == "file" else None,
            "image_key": _safe_key_from_content(content, "image_key") if msg_type == "image" else None,
        })
    except Exception:
        logger.exception("儲存訊息至資料庫失敗")


    # 2. 根據訊息類型分流處理
    # --- 處理文字訊息 ---
    if msg_type == "text":
        text = (content or "").strip()
        if not text:
            return

        # 判斷群組中是否需要 @機器人 (保留您的邏輯)
        if chat_type == "group" and getattr(settings, "REQUIRE_MENTION", True):
            if not _is_bot_mentioned(msg, settings.BOT_NAME):
                logger.info(f"群組訊息未提及機器人，已忽略。chat_id={chat_id}")
                return
            text = _strip_bot_mention(text, settings.BOT_NAME)

        # 處理指令 (保留您的邏輯)
        if text.startswith("/"):
            if text.startswith("/help"):
                reply = "指令：\n/time 現在時間\n/date 今日日期\n/summary 立即匯整昨天摘要"
                await lark_client.send_message(http, chat_id, reply)
            elif text.startswith("/time"):
                reply = utils.now_local().strftime("現在時間：%Y-%m-%d %H:%M:%S %Z")
                await lark_client.send_message(http, chat_id, reply)
            elif text.startswith("/date"):
                reply = utils.now_local().strftime("今日日期：%Y-%m-%d（%A）")
                await lark_client.send_message(http, chat_id, reply)
            elif text.startswith("/summary"):
                # 這裡可以觸發手動摘要任務，目前先回覆訊息
                await lark_client.send_message(http, chat_id, "收到手動摘要指令，此功能正在開發中。")
            return

        # --- 整合 OpenAI API 呼叫 ---
        if aclient:
            try:
                logger.info(f"正在為 chat_id: {chat_id} 呼叫 OpenAI API...")
                chat_completion = await aclient.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "你是一個樂於助人、使用繁體中文回答的助理。"},
                        {"role": "user", "content": text},
                    ],
                    model="gpt-4o-mini",
                )
                reply_text = chat_completion.choices[0].message.content.strip()
                logger.info(f"成功從 OpenAI 收到 chat_id: {chat_id} 的回應。")
            except Exception as e:
                logger.exception(f"呼叫 OpenAI API 時發生錯誤: {e}")
                reply_text = f"抱歉，AI 服務暫時無法連線。錯誤：{type(e).__name__}"
        else:
            # 如果沒有設定 OpenAI Key，則使用您原本的回覆邏輯
            reply_text = f"收到您的訊息：「{text}」。(AI 功能未啟用)"
        
        await lark_client.reply_message(http, message_id, reply_text)
        return

    # --- 處理圖片訊息 (保留您的邏輯) ---
    if msg_type == "image":
        image_key = _safe_key_from_content(content, "image_key")
        if not image_key:
            logger.warning(f"圖片訊息缺少 image_key: {message_id}")
            return
        # 您可以在這裡加入下載圖片後的處理邏輯
        # data, fname, ctype = await lark_client.get_message_resource(http, message_id, image_key, "image")
        await lark_client.reply_message(http, message_id, f"已收到您傳送的圖片！")
        return

    # --- 處理檔案訊息 (保留您的邏輯) ---
    if msg_type == "file":
        file_key = _safe_key_from_content(content, "file_key")
        if not file_key:
            logger.warning(f"檔案訊息缺少 file_key: {message_id}")
            return
        # 您可以在這裡加入下載檔案後的處理邏輯
        # data, fname, ctype = await lark_client.get_message_resource(http, message_id, file_key, "file")
        await lark_client.reply_message(http, message_id, f"已收到您傳送的檔案！")
        return


# ===================================================================
# == 輔助函式 (從您原本的 main.py 完整保留)
# ===================================================================

def _parse_msg_basic(msg: Dict[str, Any]) -> Dict[str, Any]:
    content = msg.get("content", "")
    msg_type = msg.get("message_type")
    # Lark 把 content 以 JSON 字串形式給出
    if isinstance(content, dict):
        try:
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = str(content)
    
    final_content = _extract_text_if_text(msg_type, content)

    return {
        "chat_id": msg.get("chat_id"),
        "message_id": msg.get("message_id"),
        "msg_type": msg_type,
        "chat_type": msg.get("chat_type"),
        "content": final_content,
        "create_ms": int(msg.get("create_time", 0)),
    }

def _extract_text_if_text(msg_type: Optional[str], content_str: str) -> str:
    if msg_type != "text":
        return content_str
    try:
        obj = json.loads(content_str or "{}")
        return (obj.get("text") or "").strip()
    except Exception:
        return content_str

def _safe_key_from_content(content_str: str, key: str) -> Optional[str]:
    try:
        obj = json.loads(content_str or "{}")
        return (obj.get(key) or "").strip() or None
    except Exception:
        return None

def _normalize_name(name: str) -> str:
    return (name or "").strip().lower().lstrip("@").replace(" ", "")

def _is_bot_mentioned(msg: Dict[str, Any], bot_name: str) -> bool:
    mentions: List[Dict[str, Any]] = msg.get("mentions") or []
    if not mentions:
        return False
    want = _normalize_name(bot_name)
    for m in mentions:
        if _normalize_name(m.get("name", "")) == want:
            return True
    return False

def _strip_bot_mention(text: str, bot_name: str) -> str:
    """從文本中移除 @機器人，例如 '@BOT_NAME hello' -> 'hello'"""
    mention_string = f"@{bot_name}"
    if text.strip().startswith(mention_string):
        return text.strip()[len(mention_string):].lstrip()
    return text

