# Live Server — OpenTransLive 網頁伺服器

`live_server` 是 OpenTransLive 的核心：FastAPI + Socket.IO 網頁伺服器，提供 panel 控制台、觀眾頁、即時轉錄管理、翻譯流程、字幕儲存與匯出。

語言：繁體中文（[English](README.en.md)）

完整角色、流程、API 與 FAQ 請看 [../docs/USAGE.md](../docs/USAGE.md)。本檔案只說明伺服器本身的設定、啟動與運維細節。

## 安裝

### 前置需求

- Python 3.11+
- MongoDB
- Redis
- 至少一組 AI provider API key
- （選用）Docker 與 Docker Compose
- （選用）ElevenLabs API key（即時麥克風轉錄）
- （選用）YouTube API key（YouTube 同步直播時間）

### 步驟

```bash
cd live_server
uv sync
cp app/secret/config.example.toml app/secret/config.toml
# 編輯 app/secret/config.toml
uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
```

開發模式（自動 reload）：

```bash
uv run uvicorn app:socket_app --reload --host 0.0.0.0 --port 5000
```

### Docker Compose

```bash
cp app/secret/config.example.toml app/secret/config.toml
# 編輯 app/secret/config.toml
docker-compose up -d
```

Compose 會啟動 FastAPI server、MongoDB、Redis。

## 設定

### 主設定檔 `app/secret/config.toml`

從 `app/secret/config.example.toml` 複製成 `app/secret/config.toml`，編輯下列區塊（`app/config.py` 是讀取這個 TOML 的載入器，通常不用改）：

| 區塊 | 用途 |
|---|---|
| `[settings].SECRET_KEY` | Session cookie 與安全相關 |
| `[settings].YOUTUBE_API_KEY` | 查詢 YouTube 直播開始時間 |
| `[email_settings].ADMIN_EMAILS` | 可進入 `/dashboard` 的管理員 email |
| `[email_settings].SMTP_*` | OTP 信件寄送；留空則 OTP 寫進 log（適合開發）|
| `[mongodb_settings]` | MongoDB 連線 |
| `redis_url` | Redis 連線 |
| `[realtime_settings].ELEVENLABS_API_KEY` | ElevenLabs Scribe |
| `[realtime_settings].AI_PROVIDER` | 預設修正／翻譯 provider (`openai` / `gemini` / `groq` / `cerebras`) |
| `[realtime_settings].CORRECT_PROVIDER` | （可選）修正流程專用 provider |
| `[realtime_settings].TRANSLATE_PROVIDER` | （可選）翻譯流程專用 provider |
| `[realtime_settings].TRANSLATE_LANGUAGES` | 預設翻譯目標語言 |
| `[realtime_settings].COMMON_PROMPT` | 活動背景或翻譯上下文 |
| `[realtime_settings].PARTIAL_INTERVAL` | partial 字幕 flush 間隔（秒）|
| `[realtime_settings].SKIP_CORRECTION` | 是否略過修正流程 |

> 各 AI provider 的模型與 prompt 預設在 `app/secret/models.example.toml`。要自訂時,複製成 `app/secret/models.toml` 編輯即可(存在就優先載入,否則 fallback 回 example);`models.toml` 已被 gitignore。

### 環境變數

部分運維選項仍從環境變數讀取：

| 變數 | 說明 | 預設 |
|---|---|---|
| `ENVIRONMENT` | 設為 `production` 會開啟 Secure cookie 與嚴格的 Socket.IO CORS | `development` |
| `SOCKET_CORS_ALLOWED_ORIGINS` | production 模式下的 Socket.IO 允許來源（逗號分隔）| 內建 localhost 清單 |
| `SEGMENT_WRITE_WORKERS` | MongoDB segment 寫入 worker 數 | `2` |
| `SEGMENT_WRITE_QUEUE_MAXSIZE` | 寫入佇列容量上限 | `500` |
| `SEGMENT_WRITE_METRICS_LOG_INTERVAL_SEC` | 寫入佇列 metrics log 間隔（秒）| `10` |

### Committed segment 寫入策略

完成片段以固定大小佇列寫入 MongoDB，避免每段都產生獨立 async task：

- 佇列：固定大小 `asyncio.Queue` (`SEGMENT_WRITE_QUEUE_MAXSIZE`)
- Worker：固定數量 (`SEGMENT_WRITE_WORKERS`)
- 滿載行為：佇列滿時丟棄最舊的 committed segment，以保住記憶體並保留較新內容
- Metrics：佇列深度、丟棄數、處理數、失敗數、平均寫入延遲（由 `SEGMENT_WRITE_METRICS_LOG_INTERVAL_SEC` 控制 log 頻率）

## 主要程式結構

```
app/
├── __init__.py              # FastAPI app、HTTP 路由、Socket.IO handler、SSE
├── config.py                # 設定載入器（讀取 secret/config.toml）
├── secret/                   # config.toml（機密）、models.toml（override）、*.example.toml
├── database.py              # MongoDB 連線與 collection
├── email_auth.py            # Email OTP 登入
├── http_client.py           # 共用 httpx client
├── logger_config.py         # logging 設定
├── scribe_manager.py        # ElevenLabs Scribe session 管理
├── socket_schema.py         # Socket.IO 事件 schema 驗證
├── translation_service.py   # 修正 + 翻譯流程與佇列管理
├── translators/             # 各 AI provider 實作
├── static/                  # CSS / JS / 圖示
└── templates/               # Jinja2 模板
```

## 觀眾／Panel 通訊

- **觀眾頁**（`/rt/{sid}`、`/yt/{sid}`）使用 SSE：`GET /api/session/{sid}/stream`，事件 `transcription_update`。
- **Panel** 使用 Socket.IO 做雙向控制與音訊上傳。

完整事件與 API 列表請看 [../docs/USAGE.md](../docs/USAGE.md#5-api-與即時通訊)。

## 資料儲存

詳細欄位請看 [../docs/USAGE.md](../docs/USAGE.md#6-資料儲存)。摘要：

- **MongoDB**：`rooms`（session 設定 + 擁有者）、`transcription_segments`（committed 片段）、`transcription_store`（legacy）
- **Redis**：`transcription:{sid}:list`（近期片段）、`transcription:{sid}:partial`（partial）、`transcription:{sid}:meta`、`keywords:{sid}`、`locked_keywords:{sid}`、`text_dictionary:{sid}`

## 安全性

- 觀眾頁完全公開、免登入。
- Panel／編輯／管理員 API 透過 Email OTP 登入；session-level 操作需 secret key 或 owner／co-owner 權限。
- production 模式（`ENVIRONMENT=production`）會啟用 Secure cookie 與嚴格 Socket.IO CORS 白名單。
- Socket.IO 事件以 [socket_schema.py](app/socket_schema.py) 驗證輸入。

## 疑難排解

請看 [../docs/USAGE.md#8-常見問題](../docs/USAGE.md#8-常見問題)。
