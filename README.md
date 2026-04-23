# OpenTransLive 開源即時語音翻譯系統

一個完整的即時語音轉錄和翻譯系統，具備網頁介面和 YouTube 同步支援。

![螢幕擷取畫面_15-9-2025_1231_transcribe g0v tw](https://github.com/user-attachments/assets/9a7ff25a-557d-43b7-8071-e7a6ca176c5f)
![螢幕擷取畫面_15-9-2025_115957_transcribe g0v tw](https://github.com/user-attachments/assets/6e36b33b-9d41-4734-a833-4a84fa3943cc)

## 功能特色

- **即時語音轉錄**：支援多種轉錄引擎 (WhisperX, OpenAI, Groq, ElevenLabs Scribe, Google Speech-to-Text)
- **多語言翻譯**：使用 GPT 自動翻譯成多種語言，支援上下文感知翻譯
- **WebSocket 通訊**：客戶端和伺服器間的即時雙向通訊，使用 Redis 支援橫向擴展
- **現代化網頁介面**：基於 FastAPI 的高效能網頁應用程式，支援多種檢視模式
- **YouTube 整合**：支援 YouTube 影片同步字幕，包含直播支援
- **會話管理**：建立和管理多個轉錄會話，支援持久化儲存
- **資料庫支援**：使用 MongoDB 儲存轉錄記錄，Redis 提供快取和即時通訊
- **關鍵字學習**：自動關鍵字提取和學習，提升轉錄準確性

## 專案結構

```
opentranslive/
├── live_server/            # 網頁伺服器與 API (FastAPI + Socket.IO)
│   ├── app/                # 主應用程式
│   │   ├── __init__.py     # 路由和 WebSocket 處理
│   │   ├── database.py     # MongoDB 整合
│   │   ├── translation_service.py # 翻譯流程與佇列管理
│   │   ├── translators/    # 翻譯提供者抽象與實作
│   │   ├── scribe_manager.py # ElevenLabs Scribe 管理
│   │   └── templates/      # HTML 模板
│   ├── Dockerfile          # Docker 容器設定
│   ├── docker-compose.yml  # Docker Compose 設定
│   └── README.md           # 伺服器詳細文件
├── transcribe_client/      # 標準音訊擷取與轉錄客戶端
│   ├── run.py              # 主要客戶端程式
│   └── pyproject.toml      # 客戶端依賴設定
├── realtime_client/        # 即時串流客戶端
│   ├── run.py              # 即時客戶端程式
│   ├── google_stt_v2.py    # Google STT 整合
│   ├── elevenlabs_realtime.py # ElevenLabs 整合
│   └── pyproject.toml      # 即時客戶端依賴設定
└── README.md               # 本檔案
```

## 快速開始

### 前置需求

- Python 3.11 或更高版本
- MongoDB (用於資料持久化)
- Redis (用於即時通訊和快取)
- 麥克風存取權限 (用於客戶端)
- OpenAI API 金鑰 (用於翻譯)

### 安裝步驟

1. **複製專案**:
   ```bash
   git clone <repository-url>
   cd opentranslive
   ```

2. **啟動伺服器**:
   ```bash
   cd live_server

   # 使用 Docker Compose (推薦)
   docker-compose up -d

   # 或手動設定
   uv sync
   cp .env.example .env  # 編輯 .env 設定環境變數
   uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
   ```

   詳細伺服器設定請參閱 [live_server/README.md](live_server/README.md)

3. **設定客戶端環境變數**:

   在 `transcribe_client/.env` 或 `realtime_client/.env` 中設定：
   ```bash
   OPENAI_API_KEY=your_openai_api_key
   SERVER_ENDPOINT=http://127.0.0.1:5000
   AI_MODEL=gpt-4.1-nano
   TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
   SECRET_KEY=your-secret-key
   ```

4. **啟動客戶端** (選擇其一):

   **標準客戶端** (WhisperX/OpenAI/Groq):
   ```bash
   cd transcribe_client
   uv sync
   uv run python run.py -t your_session_id
   ```

   **即時客戶端** (ElevenLabs Scribe/Google STT):
   ```bash
   cd realtime_client
   uv sync
   uv run python run.py -t your_session_id
   ```

5. **存取網頁介面**:
   - 主頁: `http://localhost:5000`
   - 即時轉錄檢視: `http://localhost:5000/rt/{session_id}`
   - YouTube 整合: `http://localhost:5000/yt/{session_id}`

## 組件說明

### Live Server (網頁伺服器)

提供網頁介面、API 端點和 WebSocket 通訊。支援多種檢視模式、即時翻譯管理和會話持久化。

**主要功能:**
- FastAPI 網頁框架
- Socket.IO WebSocket 支援
- MongoDB 資料持久化
- Redis 快取和訊息佇列
- ElevenLabs Scribe 整合
- Google Speech-to-Text 支援
- 自動翻譯服務
- QR 碼產生 (方便行動裝置存取)

完整文件請參閱: [live_server/README.md](live_server/README.md)

### Transcribe Client (標準客戶端)

標準音訊擷取與轉錄客戶端，支援批次處理模式。

**支援的轉錄引擎:**
- **WhisperX**: 本地運行，支援多種模型 (tiny 到 large-v3)
- **OpenAI Whisper API**: 雲端服務，高準確度
- **Groq**: 快速雲端轉錄服務

**主要功能:**
- 音訊擷取和語音偵測
- 可配置的錄音參數 (能量閾值、暫停時間)
- 重疊緩衝區處理
- 自動翻譯和關鍵字提取
- WebSocket 即時同步

### Realtime Client (即時客戶端)

實驗性即時串流客戶端，提供低延遲轉錄。

**支援的引擎:**
- **ElevenLabs Scribe Realtime**: 即時串流轉錄
- **Google Speech-to-Text**: Google Cloud STT 服務

**主要功能:**
- 低延遲音訊串流
- 即時轉錄顯示
- WebSocket 雙向通訊
- 自動重連機制

## 配置選項

### 通用環境變數

```bash
# API 金鑰
OPENAI_API_KEY=your_openai_api_key      # 必填 (翻譯用)
ELEVENLABS_API_KEY=your_elevenlabs_key  # ElevenLabs 使用者需填
GROQ_API_KEY=your_groq_api_key          # Groq 使用者需填

# 伺服器設定
SERVER_ENDPOINT=http://127.0.0.1:5000
SECRET_KEY=your-secret-key

# 翻譯設定
AI_MODEL=gpt-4.1-nano
TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
COMMON_PROMPT="Context about your meeting or event"
```

### 標準客戶端專用

```bash
TRANSCRIBER=whisperx                    # whisperx, openai, 或 groq
TRANSCRIBE_MODEL=large-v3               # Whisper 模型大小
RECORD_TIMEOUT=5                        # 錄音超時 (秒)
RECORD_ENERGY_THRESHOLD=150             # 語音偵測閾值
RECORD_PAUSE_THRESHOLD_MS=1000          # 句子暫停時間 (毫秒)
```

### 伺服器專用

```bash
# 資料庫
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=opentranslive
REDIS_URL=redis://localhost:6379/0

# YouTube
YOUTUBE_API_KEY=your_youtube_api_key

# Google Cloud (可選)
GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
GOOGLE_CLOUD_PROJECT=your_project_id
```

完整配置說明請參閱各組件的 README。

## 使用情境

### 會議即時轉錄

1. 啟動伺服器
2. 使用標準客戶端或即時客戶端連接麥克風
3. 參與者透過網頁介面 `/rt/{session_id}` 觀看即時轉錄和翻譯
4. 支援多語言同步顯示

### YouTube 直播字幕

1. 啟動伺服器和客戶端
2. 透過 `/yt/{session_id}` 檢視，輸入 YouTube 影片 ID
3. 影片播放時同步顯示即時字幕
4. 支援直播和錄播影片

### 遠端會議輔助

1. 在會議室設置麥克風和客戶端
2. 產生 QR 碼讓遠端參與者掃描
3. 遠端參與者透過手機/平板觀看即時轉錄
4. 多語言翻譯協助跨語言溝通

## API 使用範例

### WebSocket 客戶端

```javascript
const socket = io('http://localhost:5000');

// 加入會話
socket.emit('join_session', {
  session_id: 'your-session-id'
});

// 接收即時更新
socket.on('transcription_update', (data) => {
  console.log('轉錄文字:', data.text);
  console.log('翻譯:', data.result.translated);
});

// 送出轉錄資料（目前主流程）
socket.emit('sync', {
  id: 'your-session-id',
  text: 'Hello world',
  start_time: 1711111111.0,
  end_time: 1711111112.4,
  partial: false
});
```

### HTTP API

```bash
# 查詢 session 語言設定（需登入與權限）
curl http://localhost:5000/api/session/your-session-id/languages
```

> 注意：目前伺服器沒有 `POST /api/sync/{session_id}`。  
> 即時轉錄同步請使用 Socket.IO `sync` event。

完整 API 文件請參閱: [live_server/README.md](live_server/README.md#api-documentation)

## 技術架構

### 轉錄流程

```
麥克風 → 客戶端 (音訊處理) → 轉錄引擎 → 翻譯服務 → 伺服器 → WebSocket → 網頁介面
                                                              ↓
                                                          MongoDB
                                                          (持久化)
```

### 即時通訊

- **WebSocket**: Socket.IO 提供雙向即時通訊
- **Redis**: 支援多伺服器部署和訊息廣播
- **MongoDB**: 儲存歷史記錄和會話資料

### 翻譯系統

- **上下文感知**: 使用對話歷史提升翻譯品質
- **關鍵字學習**: 自動提取專業術語
- **非同步處理**: 不阻塞主要轉錄流程
- **多語言並行**: 同時翻譯成多種目標語言

## 系統需求

### 伺服器端

- CPU: 2+ 核心 (建議 4 核心)
- RAM: 4GB+ (建議 8GB)
- 儲存空間: 20GB+
- 網路: 穩定的網際網路連線

### 客戶端 (標準客戶端使用 WhisperX)

- CPU: 4+ 核心
- RAM: 8GB+ (large 模型需要 16GB+)
- GPU: NVIDIA GPU 搭配 CUDA (可選，大幅提升效能)

### 客戶端 (即時客戶端)

- CPU: 2+ 核心
- RAM: 2GB+
- 網路: 低延遲網際網路連線 (用於雲端 API)

## 部署建議

### 開發環境

使用本地 MongoDB 和 Redis：
```bash
# 使用 Docker Compose
cd live_server
docker-compose up -d
```

### 生產環境

建議使用:
- **MongoDB Atlas** 或自架 MongoDB 叢集
- **Redis Cloud** 或自架 Redis 叢集
- **反向代理**: Nginx 或 Caddy
- **SSL/TLS**: 使用 Let's Encrypt
- **負載平衡**: 多個伺服器實例搭配 Redis

## 疑難排解

### 常見問題

1. **轉錄延遲過高**
   - 檢查網路延遲
   - 考慮使用即時客戶端
   - 調整錄音參數 (減少 RECORD_TIMEOUT)

2. **翻譯品質不佳**
   - 設定適當的 COMMON_PROMPT 提供上下文
   - 使用更強大的 AI 模型 (如 gpt-4)
   - 累積更多關鍵字

3. **WebSocket 連線失敗**
   - 確認 Redis 正常運作
   - 檢查防火牆設定
   - 驗證 CORS 設定

4. **音訊擷取問題**
   - 確認麥克風權限
   - 調整能量閾值參數
   - 檢查音訊裝置設定

完整疑難排解指南請參閱各組件的 README。

## 授權

本專案採用 GNU AGPL v3.0 授權。詳見 LICENSE 檔案。

## 貢獻

歡迎貢獻！請隨時提出 issue 和 pull request。

### 主要貢獻者

- [SeanGau](https://github.com/SeanGau)

### 如何貢獻

1. Fork 本專案
2. 建立功能分支 (`git checkout -b feature/amazing-feature`)
3. 提交變更 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 開啟 Pull Request

## 相關連結

- [Live Server 文件](live_server/README.md) - 詳細的伺服器設定和 API 文件
- [安全性改進](SECURITY_IMPROVEMENTS.md) - 安全性相關更新
- [問題回報](https://github.com/g0v/opentranslive/issues)

## 致謝

感謝所有為本專案做出貢獻的開發者和 g0v 社群成員。
