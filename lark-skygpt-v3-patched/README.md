# 🚀 SkyGPT Bot (Lark + Railway 部署)

## 📌 專案簡介
這是一個基於 **FastAPI** + **Lark API** + **Railway** 的機器人專案，支援：
- 接收 Lark 消息事件並回覆
- 獲取消息中的圖片與文件資源
- 自動每日定時推送摘要（APScheduler）
- OpenAI 連接（可選，用於對話或摘要）

---

## 📂 專案結構
```
.
├── app/
│   ├── main.py          # FastAPI 主程序，Webhook & API
│   ├── lark_client.py   # Lark API 封裝，含消息、文件下載
├── Procfile             # Railway 啟動配置 (web + worker)
├── requirements.txt     # 依賴套件
├── .env.example         # 環境變數範例
└── README.md            # 說明文件
```

---

## ⚙️ 安裝依賴 (本地測試)
```bash
pip install -r requirements.txt
```

---

## 🔑 環境變數設定

請將以下變數新增到 **Railway**（Web & Worker 服務必須相同）：

| 變數名稱       | 說明 |
|----------------|------|
| `APP_ID`       | Lark 應用 ID |
| `APP_SECRET`   | Lark 應用 Secret |
| `BOT_NAME`     | 機器人名稱（需與 Lark 顯示名一致） |
| `DATABASE_URL` | Railway Postgres 提供的連接字串 |
| `REDIS_URL`    | Railway Redis 提供的連接字串 |
| `OPENAI_API_KEY` | OpenAI API 金鑰（可選） |
| `TIMEZONE`     | 預設 `Asia/Taipei` |
| `LOG_LEVEL`    | 預設 `INFO` |
| `LARK_BASE`    | 國際版用 `https://open.larksuite.com`，大陸版用 `https://open.feishu.cn` |
| `REQUIRE_MENTION` | 群聊是否必須 @ 機器人才響應 (`True`/`False`) |

---

## 🚀 Railway 部署

1. **Fork/上傳專案**到 GitHub  
2. Railway → New Project → **Deploy from GitHub repo**  
3. 加入 **Postgres**、**Redis** 插件，並在 **Variables** 裡補齊 `.env.example` 中的變數  
4. Railway 會自動啟動：
   - **Web** → `Procfile` 的 `web`，處理 Lark webhook  
   - **Worker** → `Procfile` 的 `worker`，執行 APScheduler  

---

## 🔗 Lark 應用設定

1. 打開 **Lark 開發者後台** → 應用管理  
2. 新增 **事件訂閱**：  
   - URL： `https://<你的 Railway domain>/webhook/lark`  
   - 關閉「事件加密」  
   - 勾選事件：`im.message.receive_v1`  
3. 點「發送測試事件」→ 確認 HTTP 200  

---

## ✅ 測試方式

- 瀏覽器開啟 `https://<你的 Railway domain>/` → 應回傳健康檢查 JSON  
- 私聊機器人輸入 `/help` → 應回覆指令列表  
- 群聊 **@機器人** 輸入 `/help` → 應回覆指令列表  
- 發送圖片/檔案 → 日誌應顯示「成功獲取資源 file_key=...」  

---

## 📜 指令一覽
- `/help` → 顯示指令列表  
- `/ping` → 測試是否存活  
- `/summary` → 請求手動輸出摘要  
- （每日自動摘要由 worker 定時推送）  

---

## 🛠 開發注意事項
- **群聊必須 @ 機器人** 才會觸發（可在 `.env` 裡改 `REQUIRE_MENTION=False` 測試）  
- 資源下載必須透過 **message_id + file_key**，詳見 `lark_client.py`  
- Railway 預設自動提供 `PORT`，不用自己設定  
