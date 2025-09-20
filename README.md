# Real-time Transcription System / 即時語音轉錄系統

A complete real-time speech transcription and translation system with web interface and YouTube synchronization support.

一個完整的即時語音轉錄和翻譯系統，具備網頁介面和 YouTube 同步支援。

![螢幕擷取畫面_15-9-2025_1231_transcribe g0v tw](https://github.com/user-attachments/assets/9a7ff25a-557d-43b7-8071-e7a6ca176c5f)
![螢幕擷取畫面_15-9-2025_115957_transcribe g0v tw](https://github.com/user-attachments/assets/6e36b33b-9d41-4734-a833-4a84fa3943cc)


## Features / 功能特色

### English
- **Real-time Speech Transcription**: Live audio capture and transcription using WhisperX, OpenAI 4o transcribe, or Groq
- **Multi-language Translation**: Automatic translation to multiple languages using GPT-4
- **WebSocket Communication**: Real-time bidirectional communication between client and server
- **Web Interface**: Modern Flask-based web application with multiple viewing modes
- **YouTube Integration**: Synchronized subtitles for YouTube videos with live streaming support
- **Server-Sent Events**: Real-time updates via SSE for live transcription display
- **Session Management**: Create and manage multiple transcription sessions
- **Audio Processing**: Advanced audio processing with overlap buffering for better accuracy
- **Context-Aware Translation**: Uses conversation context for improved translation quality
- **Keyword Learning**: Automatic keyword extraction and learning for better transcription accuracy
- **Multiple Transcriber Support**: Support for WhisperX, OpenAI, and Groq transcription engines
- **Real-time View Modes**: Grid and single-column layout options for different use cases

### 繁體中文
- **即時語音轉錄**：使用 WhisperX、OpenAI 4o transcribe 或 Groq 進行即時音訊擷取和轉錄
- **多語言翻譯**：使用 GPT 自動翻譯成多種語言
- **WebSocket 通訊**：客戶端和伺服器間的即時雙向通訊
- **網頁介面**：基於 Flask 的現代化網頁應用程式，支援多種檢視模式
- **YouTube 整合**：支援 YouTube 影片同步字幕，包含直播支援
- **伺服器推送事件**：透過 SSE 進行即時更新，顯示即時轉錄內容
- **會話管理**：建立和管理多個轉錄會話
- **音訊處理**：進階音訊處理，具備重疊緩衝區以提高準確性
- **上下文感知翻譯**：使用對話上下文以提升翻譯品質
- **關鍵字學習**：自動關鍵字提取和學習，提升轉錄準確性
- **多種轉錄引擎支援**：支援 WhisperX、OpenAI 和 Groq 轉錄引擎
- **即時檢視模式**：網格和單欄佈局選項，適用於不同使用情境

## Project Structure / 專案結構

```
realtime_transcribe/
├── realtime_transcribe_client/     # Audio capture and transcription client
│   ├── run.py                      # Main client application
│   ├── output/                     # Transcription output files
│   │   ├── current_keywords.txt    # Dynamic keyword learning file
│   │   └── YYYY-MM-DD/            # Daily transcription logs
│   ├── pyproject.toml              # Client dependencies
│   └── README.md                   # Client documentation
├── realtime_transcribe_server/     # Web server and API
│   ├── app/                        # Flask application
│   │   ├── __init__.py            # Routes and API endpoints
│   │   ├── static/                # CSS, JS, and static assets
│   │   └── templates/             # HTML templates
│   │       ├── base.html          # Base template
│   │       ├── index.html         # Main dashboard
│   │       ├── rt.html            # Real-time view
│   │       └── yt.html            # YouTube integration view
│   ├── main.py                    # Server entry point
│   ├── temp/                      # Session data storage
│   ├── pyproject.toml             # Server dependencies
│   └── README.md                  # Server documentation
└── README.md                      # This file
```

## Installation / 安裝

### Prerequisites / 前置需求

- Python 3.11 or higher / Python 3.11 或更高版本
- Microphone access / 麥克風存取權限
- OpenAI API key (for translation) / OpenAI API 金鑰（用於翻譯）
- Groq API key (optional, for Groq transcription) / Groq API 金鑰（選用，用於 Groq 轉錄）
- YouTube Data API key (optional, for YouTube integration) / YouTube Data API 金鑰（選用，用於 YouTube 整合）

### Setup / 設定

1. **Clone the repository / 複製專案**:
   ```bash
   git clone <repository-url>
   cd realtime_transcribe
   ```

2. **Install dependencies / 安裝依賴套件**:
   ```bash
   # Install server dependencies / 安裝伺服器依賴套件
   cd realtime_transcribe_server
   uv sync
   
   # Install client dependencies / 安裝客戶端依賴套件
   cd ../realtime_transcribe_client
   uv sync
   ```

3. **Configure environment variables / 設定環境變數**:
   
   Create `.env` files in both directories with the following variables:
   在兩個目錄中建立 `.env` 檔案，包含以下變數：

   ```bash
   # Client configuration / 客戶端設定
   OPENAI_API_KEY=your_openai_api_key_here
   GROQ_API_KEY=your_groq_api_key_here  # Optional / 選用
   TRANSCRIBE_MODEL=large-v3
   TRANSCRIBER=whisperx  # Options: "whisperx", "openai", "groq"
   TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
   COMMON_PROMPT=This is a meeting about software development
   SERVER_ENDPOINT=http://127.0.0.1:5000/api/sync/
   AI_MODEL=gpt-4.1-nano
   RECORD_TIMEOUT=5
   RECORD_ENERGY_THRESHOLD=150
   RECORD_PAUSE_THRESHOLD_MS=1000
   SECRET_KEY=secret-key-to-check-is-admin
   
   # Server configuration / 伺服器設定
   YOUTUBE_API_KEY=your_youtube_api_key_here  # Optional / 選用
   SECRET_KEY=secret-key-to-check-is-admin
   ```

4. **Run the system / 執行系統**:

   **Terminal 1 - Start the server / 終端機 1 - 啟動伺服器**:
   ```bash
   cd realtime_transcribe_server
   python main.py
   ```

   **Terminal 2 - Start the client / 終端機 2 - 啟動客戶端**:
   ```bash
   cd realtime_transcribe_client
   python run.py -t your_session_id
   ```

## Usage / 使用方式

### English

1. **Start the server**: The web interface will be available at `http://localhost:5000`
2. **Start the client**: Run with a session ID to begin transcription
3. **Access the web interface**: 
   - Visit `http://localhost:5000` for the main dashboard
   - Use `/rt/{session_id}` for real-time transcription view
   - Use `/yt/{session_id}` for YouTube integration view
4. **Create sessions**: Enter a unique session ID to create a new transcription session
5. **View transcriptions**: Real-time updates will appear in the web interface
6. **Switch view modes**: Use the dropdown in real-time view to switch between languages and layouts

### 繁體中文

1. **啟動伺服器**：網頁介面將在 `http://localhost:5000` 可用
2. **啟動客戶端**：使用會話 ID 執行以開始轉錄
3. **存取網頁介面**：
   - 造訪 `http://localhost:5000` 進入主要儀表板
   - 使用 `/rt/{session_id}` 檢視即時轉錄
   - 使用 `/yt/{session_id}` 檢視 YouTube 整合
4. **建立會話**：輸入唯一的會話 ID 以建立新的轉錄會話
5. **檢視轉錄**：即時更新將在網頁介面中顯示
6. **切換檢視模式**：在即時檢視中使用下拉選單切換語言和佈局

## API Documentation / API 文件

### WebSocket Events / WebSocket 事件

#### Client to Server / 客戶端到伺服器

```javascript
// Join a session / 加入會話
socket.emit('join_session', {'session_id': 'your_session_id'});

// Send transcription data / 發送轉錄資料
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

#### Server to Client / 伺服器到客戶端

```javascript
// Connection confirmation / 連線確認
socket.on('connected', (data) => {
  console.log('Connected:', data.client_id);
});

// Session join confirmation / 會話加入確認
socket.on('joined_session', (data) => {
  console.log('Joined session:', data.session_id);
});

// Real-time transcription updates / 即時轉錄更新
socket.on('transcription_update', (data) => {
  console.log('New transcription:', data);
});
```

### HTTP API Endpoints / HTTP API 端點

#### Sync Transcription Data / 同步轉錄資料

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

#### Session Views / 會話檢視

```http
GET /rt/{session_id}     # Real-time transcription view
GET /yt/{session_id}     # YouTube integration view
GET /                    # Main dashboard
```

## Configuration Options / 設定選項

### Client Configuration / 客戶端設定

| Variable / 變數 | Description / 說明 | Default / 預設值 | Options / 選項 |
|----------------|-------------------|------------------|----------------|
| `TRANSCRIBE_MODEL` | Whisper model to use / 使用的 Whisper 模型 | `large-v3` | `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3` |
| `TRANSCRIBER` | Transcription engine / 轉錄引擎 | `whisperx` | `whisperx`, `openai`, `groq` |
| `TRANSLATE_LANGUAGES` | Comma-separated target languages / 目標語言（逗號分隔） | `en,zh-tw` | IETF BCP 47 format |
| `COMMON_PROMPT` | Context prompt for better transcription / 轉錄上下文提示 | - | Free text |
| `SERVER_ENDPOINT` | Server API endpoint / 伺服器 API 端點 | `http://127.0.0.1:5000/api/sync/` | URL |
| `AI_MODEL` | AI model for translation / 翻譯用的 AI 模型 | `gpt-4.1-nano` | OpenAI model name |
| `RECORD_TIMEOUT` | Recording timeout in seconds / 錄音超時時間（秒） | `5` | Integer |
| `RECORD_ENERGY_THRESHOLD` | Energy threshold for speech detection / 語音偵測能量閾值 | `150` | Integer |
| `RECORD_PAUSE_THRESHOLD_MS` | Pause threshold in milliseconds / 暫停閾值（毫秒） | `1000` | Integer |

### Server Configuration / 伺服器設定

| Variable / 變數 | Description / 說明 | Default / 預設值 |
|----------------|-------------------|------------------|
| `YOUTUBE_API_KEY` | YouTube Data API key / YouTube Data API 金鑰 | - |

## View Modes / 檢視模式

### Real-time View (`/rt/{session_id}`)
- **English**: Dedicated view for live transcription display with auto-scrolling
- **Features**: 
  - Grid layout with multiple languages
  - Single column layout
  - Language selection dropdown
  - Real-time WebSocket updates
  - QR code generation for mobile access
- **繁體中文**：專門用於即時轉錄顯示的檢視，具備自動捲動功能
- **功能**：
  - 多語言網格佈局
  - 單欄佈局
  - 語言選擇下拉選單
  - 即時 WebSocket 更新
  - 行動裝置存取 QR 碼生成

### YouTube Integration (`/yt/{session_id}`)
- **English**: Embeds YouTube videos with synchronized subtitles and live streaming support
- **Features**:
  - YouTube player integration
  - Synchronized subtitle display
  - Live stream support
  - Automatic start time detection
- **繁體中文**：嵌入 YouTube 影片，具備同步字幕和直播支援
- **功能**：
  - YouTube 播放器整合
  - 同步字幕顯示
  - 直播支援
  - 自動開始時間偵測

## Technical Details / 技術細節

### Audio Processing / 音訊處理
- **English**: Uses overlap buffering to ensure continuous transcription accuracy
- **Features**:
  - Configurable energy threshold for speech detection
  - Adjustable pause threshold for sentence boundaries
  - Overlap buffering to prevent audio gaps
  - Multiple audio format support
- **繁體中文**：使用重疊緩衝區確保連續轉錄準確性
- **功能**：
  - 可配置的語音偵測能量閾值
  - 可調整的句子邊界暫停閾值
  - 重疊緩衝區防止音訊間隙
  - 多種音訊格式支援

### Translation Pipeline / 翻譯流程
- **English**: Context-aware translation using conversation history and extracted keywords
- **Features**:
  - Dynamic keyword learning and extraction
  - Context-aware translation using recent conversation history
  - Multi-language support with IETF BCP 47 format
  - Real-time translation with threading for performance
- **繁體中文**：使用對話歷史和提取的關鍵字進行上下文感知翻譯
- **功能**：
  - 動態關鍵字學習和提取
  - 使用近期對話歷史的上下文感知翻譯
  - IETF BCP 47 格式的多語言支援
  - 使用執行緒提升效能的即時翻譯

### Real-time Synchronization / 即時同步
- **English**: WebSocket and Server-Sent Events (SSE) for real-time updates across multiple clients
- **Features**:
  - Bidirectional WebSocket communication
  - Room-based session management
  - Automatic reconnection handling
  - Fallback to HTTP POST for reliability
- **繁體中文**：使用 WebSocket 和伺服器推送事件 (SSE) 在多重客戶端間進行即時更新
- **功能**：
  - 雙向 WebSocket 通訊
  - 基於房間的會話管理
  - 自動重連處理
  - HTTP POST 備援機制確保可靠性

### Transcription Engines / 轉錄引擎

#### WhisperX
- **English**: Advanced Whisper implementation with improved accuracy
- **Features**: Batch processing, word-level timestamps, language detection
- **繁體中文**：進階 Whisper 實作，具備提升的準確性
- **功能**：批次處理、詞級時間戳、語言偵測

#### OpenAI Whisper
- **English**: Cloud-based transcription with high accuracy
- **Features**: Server-side VAD, confidence scoring, chunking strategy
- **繁體中文**：基於雲端的高準確性轉錄
- **功能**：伺服器端 VAD、信心評分、分塊策略

#### Groq
- **English**: Fast inference with Whisper Large V3 Turbo
- **Features**: High-speed processing, verbose JSON output, prompt customization
- **繁體中文**：使用 Whisper Large V3 Turbo 的快速推論
- **功能**：高速處理、詳細 JSON 輸出、提示自訂

## Dependencies / 依賴套件

### Client / 客戶端
- `faster-whisper` - Fast Whisper implementation
- `openai` - OpenAI API client
- `groq` - Groq API client
- `pyaudio` - Audio capture
- `speechrecognition` - Speech recognition utilities
- `opencc` - Chinese text conversion
- `httpx` - HTTP client
- `whisperx` - Advanced Whisper features
- `python-socketio` - WebSocket client
- `torch` - PyTorch for WhisperX
- `torchvision` - Computer vision utilities

### Server / 伺服器
- `flask` - Web framework
- `flask-socketio` - WebSocket support for Flask
- `requests` - HTTP requests

## Data Storage / 資料儲存

### Client Output / 客戶端輸出
- **Daily logs**: `output/YYYY-MM-DD/HH-MM-SS.json` - Timestamped transcription files
- **Keywords**: `output/current_keywords.txt` - Dynamic keyword learning file
- **Format**: JSON with transcriptions, timestamps, translations, and metadata

### Server Storage / 伺服器儲存
- **Session data**: `temp/{session_id}.json` - Individual session transcriptions
- **Cache**: In-memory transcription cache with 1-hour TTL
- **YouTube data**: Cached YouTube metadata for live streams

## License / 授權

This project is open source. Please check the license file for details.
本專案為開源專案。請查看授權檔案以了解詳細資訊。

## Contributing / 貢獻

Contributions are welcome! Please feel free to submit issues and pull requests.
歡迎貢獻！請隨時提交問題和拉取請求。

## Support / 支援

For support and questions, please open an issue in the repository.
如需支援和問題，請在儲存庫中開啟問題。
