
# Lark SkyGPT (Patched)
让 Lark（飞书）群聊具备 AI 能力：问答聊天、群聊摘要、读取图片与文件（PDF/Excel）。

## 快速开始（Railway）
1. Fork 到你的 GitHub
2. Railway 新增服务 → 连接此仓库 → Python Buildpack
3. `Procfile`：
   ```
   web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
   worker: python scheduler_worker.py
   ```
4. 环境变量：
   - `OPENAI_API_KEY`（必填）
   - `OPENAI_BASE_URL`（选填）
   - `OPENAI_CHAT_MODEL`（默认 `gpt-4o-mini`）
   - `OPENAI_SUMMARY_MODEL`（默认 `gpt-4o-mini`）
   - `OPENAI_VISION_MODEL`（默认 `gpt-4o-mini`）
   - `LARK_APP_ID`、`LARK_APP_SECRET`（必填）
   - `LARK_VERIFICATION_TOKEN`（建议）
   - `LARK_ENCRYPT_KEY`（可留空）
   - `REDIS_URL`（需要订阅功能时设置）
   - `ENV`（dev/prod）

5. Lark 后台：事件订阅 URL 指向 `https://<your-app>.railway.app/lark/webhook`

## 能力
- **聊天问答**：中文专业口吻
- **群聊摘要**：发送「摘要/总结/summary」即时生成（每日自动摘要可用 APScheduler 扩展）
- **读图**：自动压缩后走 OpenAI Vision 摘要
- **读文件**：PDF（pdfminer）、Excel（pandas/openpyxl）、CSV/TXT 直接解析

## 本地
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
# http://127.0.0.1:8080/healthz
```
