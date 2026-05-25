# OpenTransLive — 為活動舉辦方設計的開源廣播式即時翻譯框架

OpenTransLive 是一套**為活動舉辦方（event organizers）打造的廣播式（one-to-many）即時翻譯框架**，而非一般的會議協作工具。完整開源（GNU AGPL v3.0），具備網頁介面與 YouTube 同步支援，可自行部署、自由修改。

設計上假設活動現場有「一位講者／一組字幕團隊」作為轉錄來源，台下或線上的「無上限觀眾」則以自己偏好的語言收聽：在大螢幕、手機網頁或 YouTube 直播字幕上同步看到即時翻譯結果。

典型使用情境：研討會、黑客松、公聽會、社群 meetup、線上直播演講等需要把單一語音來源即時翻譯給跨語言聽眾的場合。觀眾端不需要註冊、不需要登入，也沒有人數上限——只有負責產出字幕的講者／字幕員需要連線送出音訊。

語言：繁體中文（[English](README.en.md)）

![螢幕擷取畫面_15-9-2025_1231_transcribe g0v tw](https://github.com/user-attachments/assets/9a7ff25a-557d-43b7-8071-e7a6ca176c5f)
![螢幕擷取畫面_15-9-2025_115957_transcribe g0v tw](https://github.com/user-attachments/assets/6e36b33b-9d41-4734-a833-4a84fa3943cc)

## 功能特色

- **即時語音轉錄**：支援多種轉錄引擎 (WhisperX、OpenAI、Groq、ElevenLabs Scribe、Google Speech-to-Text)
- **多語言翻譯**：使用 LLM 自動翻譯成多種語言，支援上下文感知翻譯
- **使用者帳號系統**：Email OTP 登入、管理員後台、即時轉錄權限管理、session co-owner
- **Session 控制台**：`/panel/{session_id}` 提供語言、Scribe 語言、語氣、關鍵字與文字字典設定
- **歷史字幕編輯**：`/edit/{session_id}` 可修改 / 刪除已儲存片段並更新所有翻譯
- **觀眾廣播**：觀眾頁 (`/rt`、`/yt`) 透過 SSE 接收字幕，免登入、無人數上限
- **YouTube 整合**：`/yt/{session_id}` 可與 YouTube 直播或影片時間軸同步
- **匯出**：JSON 全紀錄與單一語言 SRT 匯出
- **資料庫**：MongoDB 持久化，Redis 提供快取與多伺服器擴展

## 專案結構

```
opentranslive/
├── live_server/            # FastAPI + Socket.IO 網頁伺服器
│   ├── app/                # 主應用程式
│   │   ├── __init__.py     # FastAPI app、路由、Socket.IO handler
│   │   ├── config.py       # 設定檔（從 config.example.py 複製）
│   │   ├── database.py     # MongoDB 整合
│   │   ├── email_auth.py   # Email OTP 登入
│   │   ├── scribe_manager.py      # ElevenLabs Scribe session 管理
│   │   ├── translation_service.py # 翻譯流程與佇列
│   │   ├── translators/    # 各 AI provider 實作
│   │   ├── socket_schema.py # Socket.IO 事件 schema
│   │   ├── static/         # 靜態檔案
│   │   └── templates/      # Jinja2 模板
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── README.md           # 伺服器設定細節
├── transcribe_client/      # 批次轉錄客戶端 (WhisperX / OpenAI / Groq)
├── realtime_client/        # 即時串流客戶端 (ElevenLabs / Google STT)
├── docs/USAGE.md           # 完整使用手冊（角色、流程、API、FAQ）
├── milestone.md            # 階段成果紀錄
└── README.md               # 本檔案
```

## 快速開始

### 前置需求

- Python 3.11+
- MongoDB
- Redis
- 至少一組 AI provider API key（OpenAI、Gemini、Groq 或 Cerebras 任一）
- 使用即時麥克風轉錄時需要 ElevenLabs API key

### 啟動伺服器

```bash
cd live_server

# 使用 Docker Compose (推薦)
cp app/config.example.py app/config.py
# 編輯 app/config.py，填入 SECRET_KEY、API key、SMTP 等設定
docker-compose up -d
```

或手動啟動：

```bash
cd live_server
uv sync
cp app/config.example.py app/config.py
uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
```

伺服器啟動後：

- 首頁 / 登入：`http://localhost:5000/`
- 觀眾即時字幕：`http://localhost:5000/rt/{session_id}`
- YouTube 字幕：`http://localhost:5000/yt/{session_id}`

伺服器設定細節請看 [live_server/README.md](live_server/README.md)。
完整使用流程（建立 session、開啟 panel、編輯字幕、匯出）請看 [docs/USAGE.md](docs/USAGE.md)。

### 啟動外部客戶端（選用）

`live_server` 的 panel 本身就能透過瀏覽器麥克風走 ElevenLabs Scribe 做即時轉錄，無需安裝任何客戶端。下列 client 用於需要本地推論或特定 STT provider 的場景。

**批次客戶端**（[transcribe_client/README.md](transcribe_client/README.md)）：

```bash
cd transcribe_client
uv sync
uv run python run.py -t your_session_id
```

**即時客戶端**（[realtime_client/README.md](realtime_client/README.md)）：

```bash
cd realtime_client
uv sync
uv run python run.py -t your_session_id
```

## 技術架構

### 轉錄流程

```
麥克風 → Panel (瀏覽器) → ElevenLabs Scribe → 修正/翻譯 → 伺服器 → SSE → 觀眾頁
                                                            ↓
                                                       MongoDB
                                                       (持久化)
```

### 即時通訊

- **Panel**：Socket.IO 雙向控制與音訊上傳
- **觀眾頁**：SSE 一對多廣播
- **Redis**：跨伺服器訊息廣播與快取
- **MongoDB**：committed segments 持久化

### 翻譯系統

- **上下文感知**：以最近字幕作為翻譯 context
- **關鍵字學習**：自動抽取領域術語，可手動釘選
- **文字字典**：使用者自訂直接替換
- **非同步處理**：不阻塞主轉錄流程
- **多語言並行**：同時翻譯成多種目標語言

## 系統需求

### 伺服器端

- CPU: 2 核心起 (建議 4)
- RAM: 4GB 起 (建議 8GB)
- 儲存: 20GB 起
- 穩定網際網路

### 客戶端 (使用 WhisperX 本地推論時)

- CPU: 4 核心起
- RAM: 8GB 起 (large 模型需 16GB)
- 可選 NVIDIA GPU 加速

### 客戶端 (僅雲端 API)

- CPU: 2 核心起
- RAM: 2GB 起
- 低延遲網路

## 部署建議

### 開發環境

```bash
cd live_server
docker-compose up -d
```

### 生產環境

- MongoDB Atlas 或自架叢集
- Redis Cloud 或自架叢集
- 反向代理：Nginx 或 Caddy
- SSL/TLS：Let's Encrypt
- 多伺服器水平擴展依賴 Redis pub/sub

## 授權

GNU AGPL v3.0。詳見 [LICENSE](LICENSE)。

## 貢獻

歡迎 issue 與 pull request。主要貢獻者：[SeanGau](https://github.com/SeanGau)。

1. Fork 本專案
2. 建立功能分支 (`git checkout -b feature/amazing-feature`)
3. 提交變更 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 開啟 Pull Request

## 相關文件

- [使用手冊 docs/USAGE.md](docs/USAGE.md) — 角色、流程、URL、API、資料儲存、FAQ
- [階段成果 milestone.md](milestone.md)
- [Live Server 設定 live_server/README.md](live_server/README.md)
- [問題回報](https://github.com/g0v/opentranslive/issues)

## 致謝

感謝所有為本專案做出貢獻的開發者和 g0v 社群成員。
