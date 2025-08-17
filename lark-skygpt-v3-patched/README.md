# Lark-SkyGPT (v3)

企業級 Lark 機器人：支援 Word/Excel/PDF 解析、圖片解讀、每群每日摘要（08:00）、群組需 @ 機器人才回應。

## 部署重點（Railway）
1. 於專案新增 **PostgreSQL** 與 **Redis** 服務（自動提供 `DATABASE_URL`、`REDIS_URL`）。  
2. 設定環境變數（Variables）：
   - `APP_ID`, `APP_SECRET`, `BOT_NAME`
   - `OPENAI_API_KEY`（可選，但建議）
   - `DATABASE_URL`, `REDIS_URL`
   - `TIMEZONE=Asia/Taipei`, `LOG_LEVEL=INFO`, `LARK_BASE=https://open.larksuite.com`
3. 以此專案部署；`Procfile` 已分離 web / worker，避免排程重覆。

## Webhook
- `POST /webhook/lark`（含 challenge）
- 健康檢查：`GET /healthz`

## 功能
- 私聊：直接回應訊息；
- 群組：**需要 @BOT_NAME** 才回應（檔案/圖片除外，直接處理）；
- 正確下載端點：`/open-apis/im/v1/messages/{message_id}/resources/{key}?type=file|image`；
- 每日 08:00 針對**各群**彙整「昨天 00:00–24:00」摘要。

## 本地開發
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```
