# Transcribe Client — 批次轉錄客戶端

從本機麥克風擷取音訊，以 **WhisperX / OpenAI Whisper / Groq** 做批次轉錄，再把結果送到 OpenTransLive `live_server`。

語言：繁體中文（[English](README.en.md)）

> 多數情境直接用 `live_server` panel 的瀏覽器麥克風即可，不必跑這隻 client。
> 適合需要本機 GPU 推論 (WhisperX)、或想用 OpenAI／Groq API 而非 ElevenLabs Scribe 的場合。
> 即時串流轉錄請改用 [`../realtime_client`](../realtime_client)。

## 安裝

```bash
cd transcribe_client
uv sync
cp .env.example .env
# 編輯 .env
```

## 設定 (`.env`)

| 變數 | 說明 |
|---|---|
| `SERVER_ENDPOINT` | OpenTransLive 伺服器網址 |
| `API_KEY` | 個人 API 金鑰（於 /user-dashboard 產生，帳號需擁有或共同擁有目標 room） |
| `TRANSCRIBER` | `whisperx` / `openai` / `groq` |
| `TRANSCRIBE_MODEL` | Whisper 模型名稱（例如 `deepdml/faster-whisper-large-v3-turbo-ct2`） |
| `TRANSCRIBE_DEVICE` | `cuda` 或 `cpu` |
| `OPENAI_API_KEY` | OpenAI（翻譯或 Whisper API 使用） |
| `GROQ_API_KEY` | Groq（使用 Groq 時填） |
| `AI_MODEL` | 翻譯用模型，例如 `gpt-4.1-mini` |
| `TRANSLATE_LANGUAGES` | 目標翻譯語言，例如 `zh-Hant,ja,ko,en` |
| `COMMON_PROMPT` | 活動背景或翻譯上下文 |
| `RECORD_TIMEOUT` | 錄音超時（秒）|
| `RECORD_ENERGY_THRESHOLD` | 語音偵測閾值 |
| `RECORD_PAUSE_THRESHOLD_MS` | 句子暫停判斷時間（毫秒）|

> **API_KEY 安全性**：請為這台機器準備**專用的非 admin 帳號**,只要該帳號擁有(或共同擁有)目標 room 即可推字幕 —— key 不需要更多權限。即使誤用 admin 帳號的 key,伺服器也會拒絕 key 對管理端點(建立帳號、rotate、設定)的呼叫,但最小權限仍是正確做法。key 只在連線時傳一次,不會隨每次更新重送。

## 執行

```bash
uv run python run.py -t your_session_id
```

`-t` 指定要送往哪個 session。client 會持續從預設麥克風讀取音訊、轉錄、翻譯、推送到伺服器。

## 與其他元件的關係

- `live_server` panel 的瀏覽器麥克風（ElevenLabs Scribe）：最低門檻、不需安裝任何 client。
- 本 client：需要本機推論或想用 Whisper 系列 API 時使用。
- `realtime_client`：需要更低延遲、走 ElevenLabs Scribe Realtime 或 Google STT 串流。
