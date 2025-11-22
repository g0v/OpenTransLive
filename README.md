# OpenTransLive 開源即時語音翻譯系統

一個完整的即時語音轉錄和翻譯系統，具備網頁介面和 YouTube 同步支援。

![螢幕擷取畫面_15-9-2025_1231_transcribe g0v tw](https://github.com/user-attachments/assets/9a7ff25a-557d-43b7-8071-e7a6ca176c5f)
![螢幕擷取畫面_15-9-2025_115957_transcribe g0v tw](https://github.com/user-attachments/assets/6e36b33b-9d41-4734-a833-4a84fa3943cc)


## 功能特色

- **即時語音轉錄**：
  - **標準客戶端**：支援 WhisperX、OpenAI 4o transcribe 或 Groq
  - **即時客戶端**：支援 ElevenLabs Scribe Realtime 及 Google Speech to Text
- **多語言翻譯**：使用 GPT 自動翻譯成多種語言
- **WebSocket 通訊**：客戶端和伺服器間的即時雙向通訊
- **網頁介面**：基於 Flask 的現代化網頁應用程式，支援多種檢視模式
- **YouTube 整合**：支援 YouTube 影片同步字幕，包含直播支援
- **伺服器推送事件**：透過 SSE 進行即時更新，顯示即時轉錄內容
- **會話管理**：建立和管理多個轉錄會話
- **音訊處理**：進階音訊處理，具備重疊緩衝區以提高準確性
- **上下文感知翻譯**：使用對話上下文以提升翻譯品質
- **關鍵字學習**：自動關鍵字提取和學習，提升轉錄準確性
- **即時檢視模式**：網格和單欄佈局選項，適用於不同使用情境

## 專案結構

```
realtime_transcribe/
├── transcribe_client/      # 標準音訊擷取與轉錄客戶端 (WhisperX/OpenAI/Groq)
│   ├── run.py              # 主要客戶端應用程式
│   ├── output/             # 轉錄輸出檔案
│   └── pyproject.toml      # 客戶端依賴設定
├── realtime_client/        # 實驗性即時串流客戶端 (ElevenLabs Scribe)
│   ├── run.py              # 即時客戶端應用程式
│   ├── google_stt_v2.py    # Google Speech to Text 整合
│   ├── elevenlabs_realtime.py # ElevenLabs 整合
│   └── pyproject.toml      # 即時客戶端依賴設定
├── live_server/            # 網頁伺服器與 API
│   ├── app/                # Flask 應用程式
│   ├── main.py             # 伺服器進入點
│   ├── temp/               # 會話資料儲存
│   └── pyproject.toml      # 伺服器依賴設定
└── README.md               # 本檔案
```

## 安裝

### 前置需求

- Python 3.11 或更高版本
- 麥克風存取權限
- OpenAI API 金鑰（用於翻譯）
- 其他 API 金鑰視使用的客戶端而定 (Groq, ElevenLabs 等)

### 設定

1. **複製專案**:
   ```bash
   git clone <repository-url>
   cd realtime_transcribe
   ```

2. **安裝依賴套件**:
   ```bash
   # 安裝伺服器依賴套件
   cd live_server
   uv sync
   
   # 安裝標準客戶端依賴套件
   cd ../transcribe_client
   uv sync

   # 安裝即時客戶端依賴套件
   cd ../realtime_client
   uv sync
   ```

3. **設定環境變數**:
   
   在各個目錄中 (`live_server`, `transcribe_client`, `realtime_client`) 建立 `.env` 檔案。

   **通用設定**:
   ```bash
   OPENAI_API_KEY=your_openai_api_key_here # 必填，用於翻譯
   SERVER_ENDPOINT=http://127.0.0.1:5000
   AI_MODEL=gpt-4.1-nano
   TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
   COMMON_PROMPT="This is a meeting about software development"
   SECRET_KEY=secret-key-to-check-is-admin
   ```

   **標準客戶端專用 (`transcribe_client/.env`)**:
   ```bash
   TRANSCRIBE_MODEL=large-v3
   TRANSCRIBER=whisperx  # 選項: "whisperx", "openai", "groq"
   GROQ_API_KEY=your_groq_api_key_here  # 若使用 Groq 則必填
   RECORD_TIMEOUT=5
   RECORD_ENERGY_THRESHOLD=150
   RECORD_PAUSE_THRESHOLD_MS=1000
   ```

   **即時客戶端專用 (`realtime_client/.env`)**:
   ```bash
   OPENAI_API_KEY=your_openai_api_key_here
   TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
   COMMON_PROMPT="This is a meeting about software development"
   SECRET_KEY=secret-key-to-check-is-admin
   ELEVENLABS_API_KEY=your_elevenlabs_key # 若使用 ElevenLabs Scribe 則必填
   GOOGLE_APPLICATION_CREDENTIALS=path/to/creds.json # 若使用 Google STT
   GOOGLE_CLOUD_PROJECT=your_google_cloud_project_id # 若使用 Google STT
   SERVER_ENDPOINT=http://127.0.0.1:5000
   AI_MODEL=gpt-4.1-nano
   ```

   **伺服器設定 (`live_server/.env`)**:
   ```bash
   YOUTUBE_API_KEY=your_youtube_api_key_here  # 獲取 YT 直播時間用
   SECRET_KEY=secret-key-to-check-is-admin
   ```

4. **執行系統**:

   **終端機 1 - 啟動伺服器**:
   ```bash
   cd live_server
   uv run python main.py
   ```

   **終端機 2 - 啟動客戶端 (選擇其一)**:
   
   *選項 A: 啟動標準客戶端 (WhisperX/OpenAI/Groq)*
   ```bash
   cd transcribe_client
   uv run python run.py -t your_session_id
   ```

   *選項 B: 啟動即時客戶端 (ElevenLabs/Google STT)*
   ```bash
   cd realtime_client
   uv run python run.py -t your_session_id
   ```

## 使用方式

1. **啟動伺服器**：網頁介面將在 `http://localhost:5000` 可用
2. **啟動客戶端**：使用會話 ID 執行以開始轉錄
3. **存取網頁介面**：
   - 造訪 `http://localhost:5000` 進入主要儀表板(WIP)
   - 使用 `/rt/{session_id}` 檢視即時轉錄
   - 使用 `/yt/{session_id}` 檢視 YouTube 整合
4. **建立會話**：輸入唯一的會話 ID 以建立新的轉錄會話
5. **檢視轉錄**：即時更新將在網頁介面中顯示
6. **切換檢視模式**：在即時檢視中使用下拉選單切換語言和佈局

## API 文件

### WebSocket 事件

#### 客戶端到伺服器

```javascript
// 加入會話
socket.emit('join_session', {'session_id': 'your_session_id'});

// 發送轉錄資料
socket.emit('sync', {
  'id': 'session_id',
  'message': 'transcription text',
  'start_time': 10.5,
  'end_time': 12.3,
  'created_at': '2024-01-01T12:00:00Z',
  'result': {
    'corrected': 'corrected transcription',
    'translated': {
      'en': 'Hello world',
      'zh-tw': '你好世界',
      'ja': 'こんにちは世界'
    },
    'special_keywords': ['keyword1', 'keyword2']
  }
});
```

#### 伺服器到客戶端

```javascript
// 連線確認
socket.on('connected', (data) => {
  console.log('Connected:', data.client_id);
});

// 會話加入確認
socket.on('joined_session', (data) => {
  console.log('Joined session:', data.session_id);
});

// 即時轉錄更新
socket.on('transcription_update', (data) => {
  console.log('New transcription:', data);
});
```

### HTTP API 端點

#### 同步轉錄資料

```http
POST /api/sync/{session_id}
Content-Type: application/json

{
  "message": "transcription text",
  "start_time": 10.5,
  "end_time": 12.3,
  "created_at": "2024-01-01T12:00:00Z",
  "result": {
    "corrected": "corrected transcription",
    "translated": {
      "en": "Hello world",
      "zh-tw": "你好世界",
      "ja": "こんにちは世界"
    },
    "special_keywords": ["keyword1", "keyword2"]
  }
}
```

#### 會話檢視

```http
GET /rt/{session_id}     # 即時轉錄檢視
GET /yt/{session_id}     # YouTube 整合檢視
GET /                    # 主要儀表板
```

## 設定選項

### 客戶端設定

| 變數 | 說明 | 預設值 | 選項 |
|----------------|-------------------|------------------|----------------|
| `OPENAI_API_KEY` | OpenAI API 金鑰 | - | 字串 (必填) |
| `TRANSCRIBE_MODEL` | 使用的 Whisper 模型 | `large-v3` | `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3` |
| `TRANSCRIBER` | 轉錄引擎 (標準客戶端) | `whisperx` | `whisperx`, `openai`, `groq` |
| `GROQ_API_KEY` | Groq API 金鑰 | - | 字串 (若使用 Groq) |
| `TRANSLATE_LANGUAGES` | 目標語言（逗號分隔） | `en,zh-tw` | IETF BCP 47 格式 |
| `COMMON_PROMPT` | 轉錄上下文提示 | - | 自由文字 |
| `SERVER_ENDPOINT` | 伺服器 API 端點 | `http://127.0.0.1:5000` | URL |
| `SECRET_KEY` | 伺服器通訊密鑰 | - | 字串 |
| `AI_MODEL` | 翻譯用的 AI 模型 | `gpt-4.1-nano` | OpenAI 模型名稱 |
| `RECORD_TIMEOUT` | 錄音超時時間（秒） | `5` | 整數 |
| `RECORD_ENERGY_THRESHOLD` | 語音偵測能量閾值 | `150` | 整數 |
| `RECORD_PAUSE_THRESHOLD_MS` | 暫停閾值（毫秒） | `1000` | 整數 |
| `ELEVENLABS_API_KEY` | ElevenLabs API 金鑰 | - | 字串 (僅即時客戶端) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google Cloud 憑證路徑 | - | 檔案路徑 (僅即時客戶端) |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud 專案 ID | - | 字串 (僅即時客戶端) |


### 伺服器設定

| 變數 | 說明 | 預設值 |
|----------------|-------------------|------------------|
| `YOUTUBE_API_KEY` | YouTube Data API 金鑰 | - |

## 檢視模式

### 即時檢視 (`/rt/{session_id}`)
專門用於即時轉錄顯示的檢視，具備自動捲動功能。
- **功能**：
  - 多語言網格佈局
  - 單欄佈局
  - 語言選擇下拉選單
  - 即時 WebSocket 更新
  - 行動裝置存取 QR 碼生成

### YouTube 整合 (`/yt/{session_id}`)
嵌入 YouTube 影片，具備同步字幕和直播支援。
- **功能**：
  - YouTube 播放器整合
  - 同步字幕顯示
  - 直播支援
  - 自動開始時間偵測

## 技術細節

### 音訊處理
使用重疊緩衝區確保連續轉錄準確性。
- **功能**：
  - 可配置的語音偵測能量閾值
  - 可調整的句子邊界暫停閾值
  - 重疊緩衝區防止音訊間隙
  - 多種音訊格式支援

### 翻譯流程
使用對話歷史和提取的關鍵字進行上下文感知翻譯。
- **功能**：
  - 動態關鍵字學習和提取
  - 使用近期對話歷史的上下文感知翻譯
  - IETF BCP 47 格式的多語言支援
  - 使用執行緒提升效能的即時翻譯

### 即時同步
使用 WebSocket 和伺服器推送事件 (SSE) 在多重客戶端間進行即時更新。
- **功能**：
  - 雙向 WebSocket 通訊
  - 基於房間的會話管理
  - 自動重連處理
  - HTTP POST 備援機制確保可靠性

## 依賴套件

### 標準客戶端 (`transcribe_client`)
- `faster-whisper` - 快速 Whisper 實作
- `openai` - OpenAI API 客戶端
- `groq` - Groq API 客戶端
- `pyaudio` - 音訊擷取
- `speechrecognition` - 語音識別工具
- `opencc` - 中文簡繁轉換
- `httpx` - HTTP 客戶端
- `whisperx` - 進階 Whisper 功能
- `python-socketio` - WebSocket 客戶端
- `torch` - 用於 WhisperX 的 PyTorch

### 即時客戶端 (`realtime_client`)
- `elevenlabs` - ElevenLabs SDK
- `websockets` - WebSocket 通訊
- `pyaudio` - 音訊擷取
- `opencc` - 中文簡繁轉換

### 伺服器 (`live_server`)
- `flask` - 網頁框架
- `flask-socketio` - Flask 的 WebSocket 支援
- `requests` - HTTP 請求

## 授權

本專案為開源專案。請查看授權檔案以了解詳細資訊。

## 貢獻

歡迎貢獻！請隨時提出 issue 和 pull request。

- [SeanGau](https://github.com/SeanGau)

