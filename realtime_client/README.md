# Realtime Client — 即時串流轉錄客戶端

從本機麥克風擷取音訊，**串流送往 ElevenLabs Scribe Realtime 或 Google Speech-to-Text**，再把字幕推送回 OpenTransLive `live_server`。延遲較 [`../transcribe_client`](../transcribe_client) 的批次模式低。

語言：繁體中文（[English](README.en.md)）

> 多數情境直接用 `live_server` panel 的瀏覽器麥克風即可，不必跑這隻 client。
> 本 client 適合需要從非瀏覽器環境推音訊、或想用 Google STT 而非 ElevenLabs 的情況。

## 安裝

```bash
cd realtime_client
uv sync
cp .env.example .env
# 編輯 .env
```

## 設定 (`.env`)

| 變數 | 說明 |
|---|---|
| `SERVER_ENDPOINT` | OpenTransLive 伺服器網址 |
| `SECRET_KEY` | session secret key |
| `ELEVENLABS_API_KEY` | ElevenLabs Scribe Realtime |
| `GOOGLE_API_KEY` | Google API key |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google service account JSON 路徑 |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud 專案 ID |
| `OPENAI_API_KEY` | OpenAI（翻譯使用） |
| `TRANSLATE_LANGUAGES` | 目標翻譯語言，例如 `en-US,cmn-Hant-TW` |
| `COMMON_PROMPT` | 活動背景或翻譯上下文 |

## 執行

```bash
# ElevenLabs Scribe Realtime（預設）
uv run python run.py -t your_session_id

# 切換到 Google STT
uv run python run.py -t your_session_id -s google
```

旗標：

- `-t / --target-sid`：目標 session id
- `-s / --service`：`elevenlabs`（預設）或 `google`

## 與其他元件的關係

- `live_server` panel 的瀏覽器麥克風：免安裝、最低門檻，內建走 ElevenLabs Scribe。
- 本 client：需要從伺服器主機或其他非瀏覽器環境送音訊時使用，可選 Google STT。
- `transcribe_client`：本機 WhisperX／OpenAI／Groq 批次推論。
