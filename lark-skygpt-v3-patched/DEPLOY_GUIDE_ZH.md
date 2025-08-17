# Lark-SkyGPT 部署指南（GitHub + Railway）

> 本指南針對目前倉庫結構（FastAPI + Gunicorn + APScheduler）與 `Procfile`（web/worker 分離）撰寫，已驗證可在 Railway 正常運作。

## 一、準備工作
- 一個 **Lark/飛書 自建應用**（獲取 `APP_ID`、`APP_SECRET`）。
- 一個 **GitHub** 帳號與空的私有/公開倉庫。
- 一個 **Railway** 帳號。

## 二、GitHub 操作（從本地推送）
1. 下載此專案（或使用你現有的目錄）。
2. 初始化 Git：
   ```bash
   git init
   git add .
   git commit -m "init lark-skygpt v3"
   ```
3. 建立 GitHub 倉庫（例如 `lark-skygpt-v3`），把遠端加上：
   ```bash
   git remote add origin https://github.com/<你的帳號>/lark-skygpt-v3.git
   git branch -M main
   git push -u origin main
   ```

> 如需上傳大檔案，請改用 Git LFS。

## 三、Lark（國際版）開發者配置
1. 建立自建應用並記下 `APP_ID`、`APP_SECRET`。
2. 應用權限（**至少**）：
   - 「發送訊息」：`im:message`
   - 「讀取訊息」：`im:message:readonly`（用於拉取歷史訊息做每日摘要）
   - 「檔案下載」：`im:message.resource`
3. 事件訂閱：啟用 `消息接收（im.message.receive_v1）`。
4. 加機器人到目標群組，群聊互動時**需要 @BOT_NAME** 才回覆；私聊直接回覆。
5. 如果你使用 **飛書中國版**，請把環境變數 `LARK_BASE` 改成 `https://open.feishu.cn`（本倉庫預設 `https://open.larksuite.com`）。

## 四、Railway 部署
> 我們會建立 **兩個服務（Service）**：
> - `web`：FastAPI + Gunicorn（Webhook、健康檢查 /healthz）
> - `worker`：APScheduler 定時任務（每日 08:00 逐群摘要）

### A. 建專案並連 GitHub
1. 登入 Railway → New Project → Deploy from GitHub → 選擇 `lark-skygpt-v3` 倉庫。
2. 等待建置完成（Nixpacks 會自動偵測 Python，`Procfile` 會設定 web 指令）。

### B. 新增附加服務
1. 在該 Project 內新增 **PostgreSQL**（Railway 內建）→ 建立完成後會提供環境變數（如 `DATABASE_URL`）。
2. 新增 **Redis**（Railway 內建）→ 取得 `REDIS_URL`。

> **注意**：Railway 的 `DATABASE_URL` 通常是 `postgresql://...`，本專案已在 `app/database.py`
> 內自動轉成 `postgresql+asyncpg://...`，無需手動修改。

### C. 設定環境變數（Variables）
到 `web` 服務與 `worker` 服務 **都**新增以下變數（值一致）：

| 變數名 | 說明 | 範例 |
|---|---|---|
| `APP_ID` | Lark 應用 App ID | `cli_a...` |
| `APP_SECRET` | Lark 應用 App Secret | `xxxx` |
| `BOT_NAME` | 在群組被 @ 的名稱 | `Skygpt` |
| `OPENAI_API_KEY` | OpenAI 金鑰（可選，但建議） | `sk-...` |
| `DATABASE_URL` | Railway PG 自動提供 | `postgresql://...` |
| `REDIS_URL` | Railway Redis 自動提供 | `redis://default:pass@host:port/0` |
| `TIMEZONE` | 時區 | `Asia/Taipei` |
| `LOG_LEVEL` | 日誌等級 | `INFO` |
| `LARK_BASE` | Lark/飛書開放平臺 | `https://open.larksuite.com` 或 `https://open.feishu.cn` |

> `web` 服務會自動使用 `Procfile` 裡的：
> ```
> web: gunicorn -w 2 -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${PORT} --timeout 120
> ```
> `worker` 服務請在 Railway 內 **另建一個 Service** 指向同一 GitHub 倉庫，並把 **Start Command** 改成：
> ```
> python -u scheduler_worker.py
> ```

### D. 設定 Webhook URL
1. Railway `web` 服務部署後會有一個公開 URL，例如：`https://<subdomain>.up.railway.app`。
2. 在 Lark 開發者後台 → 事件訂閱 URL 設為：  
   `https://<subdomain>.up.railway.app/webhook/lark`
3. 測試：
   - 瀏覽 `https://<subdomain>.up.railway.app/healthz` 應得到 JSON 狀態。
   - `openai` 欄位為 `true/false` 代表是否讀到了 `OPENAI_API_KEY`。

## 五、常見錯誤與排查
1. **401 tenant_access_token**：`APP_ID/APP_SECRET` 錯誤，或 `LARK_BASE` 指到錯的域名（國際/中國）。
2. **Redis 連線錯誤**：`REDIS_URL` 必須包含密碼（Railway 內建 Redis 會給 `redis://default:<password>@host:port/0`）。
3. **Postgres 驅動錯誤**：若看到「找不到 async driver」訊息，確認 `DATABASE_URL` 沒有被手動改壞。
4. **OpenAI 無回應/回退**：沒設 `OPENAI_API_KEY` 時，文字回覆會使用「降級」模式（回傳截斷內容）。
5. **群組不回覆**：群聊必須 **@BOT_NAME**；或權限不足（需開通 `im:message` / `im:message:readonly` / `im:message.resource`）。
6. **每日摘要時間**：預設 08:00（`scheduler_worker.py`），可以把 `CronTrigger` 時間改成你需要的時區時間。

## 六、開發 & 本地調試
> 本專案默認使用 PostgreSQL/Redis。若需本地快速試跑，建議使用 Docker Compose 或把 `.env` 指向雲端 PG/Redis。

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
# 打開 http://127.0.0.1:8080/healthz
```

---

若你需要 **禁用事件簽名/加密**，請在 Lark 後台把「加密密鑰」留空即可；目前程式會直接回傳 `challenge` 完成驗證。
如需新增簽名驗證/消息解密，我可以幫你補上中間件。