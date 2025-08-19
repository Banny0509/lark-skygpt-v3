
# 部署指引（中文）

## 环境变量
| 变量 | 说明 | 必填 |
|---|---|---|
| OPENAI_API_KEY | OpenAI Key | ✅ |
| OPENAI_BASE_URL | 自定义 API 入口 |  |
| OPENAI_CHAT_MODEL | 默认 gpt-4o-mini |  |
| OPENAI_SUMMARY_MODEL | 默认 gpt-4o-mini |  |
| OPENAI_VISION_MODEL | 默认 gpt-4o-mini |  |
| LARK_APP_ID | Lark App ID | ✅ |
| LARK_APP_SECRET | Lark App Secret | ✅ |
| LARK_VERIFICATION_TOKEN | 验证 Token | 建议 |
| LARK_ENCRYPT_KEY | 加密 Key | 可空 |
| REDIS_URL | Redis 连接串 | 选填 |
| ENV | dev / prod | 选填 |

## Railway
- 进程：
  - `web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  - `worker: python scheduler_worker.py`

## Lark 事件
- URL：`https://<your>.railway.app/lark/webhook`
- 权限：`im:message`, `im:message:send`, `im:files:read`

## 常见问题
- **module 'app.lark_client' has no attribute 'download_and_extract_text'** → 已补齐。
- **无 LLM** → 设置 `OPENAI_API_KEY`。
- **PDF 乱码** → 扫描件请改图片并走 Vision；或后续接 OCR。
- **Excel 读取失败** → 现已内置 pandas/openpyxl。

