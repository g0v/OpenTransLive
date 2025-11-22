# Real-time Transcription Server

A Flask-based web application that provides real-time transcription synchronization with multiple viewing modes. This server supports both YouTube video embedding with synchronized subtitles and a dedicated real-time transcription view.

## Features

- **Real-time Transcription Sync**: Server-Sent Events (SSE) for live transcription updates
- **Multiple View Modes**:
  - **YouTube Embed View**: Embed YouTube videos with synchronized subtitles
  - **Real-time View**: Dedicated view for live transcription display
- **Multi-language Support**: Display transcriptions in different languages
- **Session Management**: Create and manage transcription sessions with unique IDs
- **RESTful API**: Simple API for syncing transcription data
- **Modern UI**: Clean, responsive interface built with Tailwind CSS

## Project Structure

```
realtime_transcribe_server/
├── app/
│   ├── __init__.py          # Flask application and routes
│   ├── static/              # Static assets
│   └── templates/           # HTML templates
│       ├── base.html        # Base template
│       ├── index.html       # Main dashboard
│       ├── rt.html          # Real-time view
│       └── yt.html          # YouTube embed view
├── main.py                  # Application entry point
├── pyproject.toml          # Project dependencies
└── temp/                   # Temporary session data storage
```

## Installation

### Prerequisites

- Python 3.11 or higher
- uv (recommended) or pip

### Setup

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd realtime_transcribe_server
   ```

2. **Install dependencies**:
   ```bash
   # Using uv (recommended)
   uv sync
   
   # Or using pip
   pip install -r requirements.txt
   ```

3. **Run the application**:
   ```bash
   python main.py
   ```

The server will start on `http://localhost:5000`

## Usage

### Creating a Session

1. Open the application in your browser
2. Enter a unique session ID
3. Choose your preferred view mode:
   - **YouTube Embed View**: For videos with synchronized subtitles
   - **Real-time View**: For live transcription display

### API Endpoints

#### Sync Transcription Data
```http
POST /api/sync/{session_id}
Content-Type: application/json

{
  "message": "transcription text",
  "start_time": 10.5,
  "end_time": 12.3,
  "created_at": "2024-01-01T12:00:00Z",
  "result": {
    "translated": {
      "en": "Hello world",
      "zh": "你好世界"
    }
  }
}
```

#### Server-Sent Events
```http
GET /api/sse/{session_id}
```

Returns real-time updates for the specified session.

### View Modes

#### YouTube Embed View (`/yt/{session_id}`)
- Embeds YouTube videos using the session ID as video ID
- Displays synchronized subtitles based on video playback time
- Supports time offset adjustment
- Real-time updates via SSE

#### Real-time View (`/rt/{session_id}`)
- Dedicated view for live transcription display
- Shows the last 100 transcription entries
- Language selection for multi-language support
- Auto-scrolling display

## Configuration

The application uses minimal configuration and stores session data in the `temp/` directory. Each session is saved as a JSON file with the session ID as the filename.

## Development

### Running in Development Mode
```bash
python main.py
```

The application runs with debug mode enabled by default.

### Project Dependencies

- **Flask**: Web framework
- **Python 3.11+**: Required for modern Python features

## API Response Format

### Sync Response
```json
{
  "status": "success",
  "temp_file": "session_id.json"
}
```

### SSE Event Format
```json
{
  "type": "update",
  "data": {
    "transcriptions": [
      {
        "message": "transcription text",
        "start_time": 10.5,
        "end_time": 12.3,
        "created_at": "2024-01-01T12:00:00Z",
        "result": {
          "translated": {
            "en": "Hello world",
            "zh": "你好世界"
          }
        }
      }
    ]
  }
}
```

## License



