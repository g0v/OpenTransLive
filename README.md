# Real-time Transcription System / 即時語音轉錄系統

A complete real-time speech transcription and translation system with web interface and YouTube synchronization support.

一個完整的即時語音轉錄和翻譯系統，具備網頁介面和 YouTube 同步支援。

## Features / 功能特色

### English
- **Real-time Speech Transcription**: Live audio capture and transcription using WhisperX or OpenAI API
- **Multi-language Translation**: Automatic translation to multiple languages using GPT-4
- **Web Interface**: Modern Flask-based web application with multiple viewing modes
- **YouTube Integration**: Synchronized subtitles for YouTube videos with live streaming support
- **Server-Sent Events**: Real-time updates via SSE for live transcription display
- **Session Management**: Create and manage multiple transcription sessions
- **Audio Processing**: Advanced audio processing with overlap buffering for better accuracy
- **Context-Aware Translation**: Uses conversation context for improved translation quality
- **Keyword Learning**: Automatic keyword extraction and learning for better transcription accuracy

### 繁體中文
- **即時語音轉錄**：使用 WhisperX 或 OpenAI API 進行即時音訊擷取和轉錄
- **多語言翻譯**：使用 GPT 自動翻譯成多種語言
- **網頁介面**：基於 Flask 的現代化網頁應用程式，支援多種檢視模式
- **YouTube 整合**：支援 YouTube 影片同步字幕，包含直播支援
- **伺服器推送事件**：透過 SSE 進行即時更新，顯示即時轉錄內容
- **會話管理**：建立和管理多個轉錄會話
- **音訊處理**：進階音訊處理，具備重疊緩衝區以提高準確性
- **上下文感知翻譯**：使用對話上下文以提升翻譯品質
- **關鍵字學習**：自動關鍵字提取和學習，提升轉錄準確性

## Project Structure / 專案結構

```
realtime_transcribe/
├── realtime_transcribe_client/     # Audio capture and transcription client
│   ├── run.py                      # Main client application
│   ├── output/                     # Transcription output files
│   └── pyproject.toml              # Client dependencies
├── realtime_transcribe_server/     # Web server and API
│   ├── app/                        # Flask application
│   │   ├── __init__.py            # Routes and API endpoints
│   │   ├── static/                # CSS, JS, and static assets
│   │   └── templates/             # HTML templates
│   ├── main.py                    # Server entry point
│   ├── temp/                      # Session data storage
│   └── pyproject.toml             # Server dependencies
└── README.md                      # This file
```

## Installation / 安裝

### Prerequisites / 前置需求

- Python 3.11 or higher / Python 3.11 或更高版本
- Microphone access / 麥克風存取權限
- OpenAI API key (for translation) / OpenAI API 金鑰（用於翻譯）
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
   TRANSCRIBE_MODEL=large-v3
   TRANSCRIBER=whisperx  # or "openai"
   TRANSLATE_LANGUAGES=zh-Hant,ja,ko,en
   COMMON_PROMPT=This is a meeting about software development
   SERVER_ENDPOINT=http://127.0.0.1:5000/api/sync/
   
   # Server configuration / 伺服器設定
   YOUTUBE_API_KEY=your_youtube_api_key_here  # Optional / 選用
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

### 繁體中文

1. **啟動伺服器**：網頁介面將在 `http://localhost:5000` 可用
2. **啟動客戶端**：使用會話 ID 執行以開始轉錄
3. **存取網頁介面**：
   - 造訪 `http://localhost:5000` 進入主要儀表板
   - 使用 `/rt/{session_id}` 檢視即時轉錄
   - 使用 `/yt/{session_id}` 檢視 YouTube 整合
4. **建立會話**：輸入唯一的會話 ID 以建立新的轉錄會話
5. **檢視轉錄**：即時更新將在網頁介面中顯示

## API Documentation / API 文件

### Sync Transcription Data / 同步轉錄資料

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

### Server-Sent Events / 伺服器推送事件

```http
GET /api/sse/{session_id}
```

Returns real-time updates for the specified session.
回傳指定會話的即時更新。

## Configuration Options / 設定選項

### Client Configuration / 客戶端設定

| Variable / 變數 | Description / 說明 | Default / 預設值 |
|----------------|-------------------|------------------|
| `TRANSCRIBE_MODEL` | Whisper model to use / 使用的 Whisper 模型 | `large-v3` |
| `TRANSCRIBER` | Transcription engine / 轉錄引擎 | `whisperx` |
| `TRANSLATE_LANGUAGES` | Comma-separated target languages / 目標語言（逗號分隔） | `en,zh-tw` |
| `COMMON_PROMPT` | Context prompt for better transcription / 轉錄上下文提示 | - |
| `SERVER_ENDPOINT` | Server API endpoint / 伺服器 API 端點 | `http://127.0.0.1:5000/api/sync/` |

### Server Configuration / 伺服器設定

| Variable / 變數 | Description / 說明 | Default / 預設值 |
|----------------|-------------------|------------------|
| `YOUTUBE_API_KEY` | YouTube Data API key / YouTube Data API 金鑰 | - |

## View Modes / 檢視模式

### Real-time View (`/rt/{session_id}`)
- **English**: Dedicated view for live transcription display with auto-scrolling
- **繁體中文**：專門用於即時轉錄顯示的檢視，具備自動捲動功能

### YouTube Integration (`/yt/{session_id}`)
- **English**: Embeds YouTube videos with synchronized subtitles and live streaming support
- **繁體中文**：嵌入 YouTube 影片，具備同步字幕和直播支援

## Technical Details / 技術細節

### Audio Processing / 音訊處理
- **English**: Uses overlap buffering to ensure continuous transcription accuracy
- **繁體中文**：使用重疊緩衝區確保連續轉錄準確性

### Translation Pipeline / 翻譯流程
- **English**: Context-aware translation using conversation history and extracted keywords
- **繁體中文**：使用對話歷史和提取的關鍵字進行上下文感知翻譯

### Real-time Synchronization / 即時同步
- **English**: Server-Sent Events (SSE) for real-time updates across multiple clients
- **繁體中文**：使用伺服器推送事件 (SSE) 在多重客戶端間進行即時更新

## Dependencies / 依賴套件

### Client / 客戶端
- `faster-whisper` - Fast Whisper implementation
- `openai` - OpenAI API client
- `pyaudio` - Audio capture
- `speechrecognition` - Speech recognition utilities
- `opencc` - Chinese text conversion
- `httpx` - HTTP client
- `whisperx` - Advanced Whisper features

### Server / 伺服器
- `flask` - Web framework
- `requests` - HTTP requests

## License / 授權

This project is open source. Please check the license file for details.
本專案為開源專案。請查看授權檔案以了解詳細資訊。

## Contributing / 貢獻

Contributions are welcome! Please feel free to submit issues and pull requests.
歡迎貢獻！請隨時提交問題和拉取請求。

## Support / 支援

For support and questions, please open an issue in the repository.
如需支援和問題，請在儲存庫中開啟問題。
