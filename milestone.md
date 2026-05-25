# OpenTransLive 階段成果與使用說明

本文件整理 OpenTransLive 目前已完成的階段成果、主要使用流程、部署方式、管理功能與驗證項目。內容以實際操作為主，避免使用不明確的描述。

## 1. 專案定位

OpenTransLive 是一套開源的廣播式即時轉錄與翻譯系統，主要用於活動、直播、講座、工作坊與公開會議。

系統假設有一個主要音訊來源，由講者、主持人或字幕工作人員透過管理介面送出音訊；觀眾透過網頁即時觀看轉錄與翻譯內容。觀眾端不需要登入。

## 2. 使用角色

| 角色 | 需要登入 | 主要用途 |
|---|---:|---|
| 系統管理員 | 是 | 管理使用者是否可使用即時轉錄功能。 |
| Session 擁有者 | 是 | 建立與管理 session、開啟 panel、設定語言、字詞與協作者。 |
| Session 協作者 | 是 | 進入已授權 session 的 panel，協助操作與調整設定。 |
| 觀眾 | 否 | 透過 `/rt/{session_id}` 或 `/yt/{session_id}` 觀看字幕。 |

## 3. 核心流程

### 3.1 建立與管理 session

1. 使用者開啟 `/login`。
2. 輸入 email 並完成 OTP 驗證。
3. 一般使用者進入 `/user-dashboard`。
4. 使用者建立或開啟既有 session。
5. 進入 `/panel/{session_id}` 作為該 session 的控制台。

補充：

- 第一位建立 session 的登入使用者會成為主要擁有者。
- 主要擁有者可新增 co-owner。
- 若 panel 管理鎖超時，擁有者或 co-owner 可重新取得控制權。

### 3.2 開始即時轉錄

1. Session 擁有者或 co-owner 進入 `/panel/{session_id}`。
2. 確認該帳號已被系統管理員允許使用 realtime transcription。
3. 在 panel 中設定：
   - 翻譯目標語言。
   - Scribe 偵測語言，留空代表自動偵測。
   - 翻譯語氣。
   - 關鍵字。
   - 文字字典。
4. 開啟麥克風。
5. 系統將音訊送往即時轉錄服務，取得文字後進行修正與翻譯。
6. 完成的字幕片段會寫入 Redis 與 MongoDB。
7. 觀眾端透過 SSE 接收更新。

### 3.3 觀眾觀看字幕

一般即時字幕頁：

```text
/rt/{session_id}
```

YouTube 同步字幕頁：

```text
/yt/{session_id}
```

觀眾端特性：

- 不需要登入。
- 使用 Server-Sent Events 接收即時字幕。
- 可依頁面提供的介面切換顯示語言或版面。
- `/yt/{session_id}` 可搭配 YouTube 直播或影片使用。

### 3.4 編輯歷史字幕

Session 擁有者或 co-owner 可開啟：

```text
/edit/{session_id}
```

可執行的操作：

- 修改已儲存片段的 corrected text。
- 修改各語言翻譯內容。
- 刪除不需要的字幕片段。
- 編輯後會更新 MongoDB；若 Redis 快取存在，也會同步更新快取。

### 3.5 匯出字幕

匯出整份 JSON：

```text
/download/{session_id}
```

匯出單一語言 SRT：

```text
/download/{session_id}/srt/{lang}
```

範例：

```text
/download/demo-session/srt/zh-Hant-TW
/download/demo-session/srt/en-US
```

限制：

- 若 session 有擁有者，匯出需要擁有者或 co-owner 權限。
- SRT 只會輸出指定語言已存在的翻譯片段。

## 4. 部署與啟動

### 4.1 前置需求

- Python 3.11 或以上。
- `uv`。
- MongoDB。
- Redis。
- 可用的 AI provider API key，例如 OpenAI、Gemini、Groq 或 Cerebras。
- 若使用 ElevenLabs Scribe，需要 ElevenLabs API key。
- 若使用 YouTube 同步直播時間，需要 YouTube API key。

### 4.2 伺服器設定

進入伺服器目錄：

```bash
cd live_server
```

安裝依賴：

```bash
uv sync
```

建立設定檔：

```bash
cp app/config.example.py app/config.py
```

需要確認的主要設定：

| 設定 | 用途 |
|---|---|
| `SETTINGS.SECRET_KEY` | Session cookie 與安全相關用途。 |
| `SETTINGS.YOUTUBE_API_KEY` | 查詢 YouTube 直播開始時間。 |
| `EMAIL_SETTINGS.ADMIN_EMAILS` | 可進入 `/dashboard` 的管理員 email。 |
| `EMAIL_SETTINGS.SMTP_*` | OTP email 寄送設定。 |
| `MONGODB_SETTINGS` | MongoDB 連線設定。 |
| `REDIS_URL` | Redis 連線設定。 |
| `REALTIME_SETTINGS.ELEVENLABS_API_KEY` | ElevenLabs Scribe 即時轉錄。 |
| `REALTIME_SETTINGS.AI_PROVIDER` | 預設修正與翻譯 provider。 |
| `REALTIME_SETTINGS.TRANSLATE_LANGUAGES` | 預設翻譯語言。 |
| `REALTIME_SETTINGS.COMMON_PROMPT` | 活動背景或翻譯上下文。 |
| `REALTIME_SETTINGS.SKIP_CORRECTION` | 是否略過修正流程。 |

啟動伺服器：

```bash
uv run uvicorn app:socket_app --host 0.0.0.0 --port 5000
```

開發時可使用 reload：

```bash
uv run uvicorn app:socket_app --reload --host 0.0.0.0 --port 5000
```

### 4.3 使用 Docker Compose

```bash
cd live_server
docker-compose up -d
```

Docker Compose 會啟動：

- FastAPI server。
- MongoDB。
- Redis。

仍需確認 `app/config.py` 已存在，且 API key 與 email 設定正確。

## 5. 主要網址

| 路徑 | 用途 | 權限 |
|---|---|---|
| `/` | 首頁與入口。 | 公開 |
| `/login` | Email OTP 登入。 | 公開 |
| `/logout` | 登出。 | 已登入 |
| `/dashboard` | 系統管理員後台。 | 系統管理員 |
| `/user-dashboard` | 使用者 session 清單。 | 已登入 |
| `/panel/{session_id}` | Session 控制台。 | 擁有者或 co-owner |
| `/rt/{session_id}` | 即時字幕觀看頁。 | 公開 |
| `/yt/{session_id}` | YouTube 字幕觀看頁。 | 公開 |
| `/edit/{session_id}` | 歷史字幕編輯頁。 | 擁有者或 co-owner |
| `/download/{session_id}` | 匯出 JSON。 | 視 session 權限而定 |
| `/download/{session_id}/srt/{lang}` | 匯出單語言 SRT。 | 視 session 權限而定 |

## 6. Panel 可調整項目

| 項目 | 說明 |
|---|---|
| Translation languages | 設定字幕要翻譯成哪些語言。 |
| Scribe language | 指定語音辨識語言；留空代表自動偵測。 |
| Translate tone | 指定翻譯語氣，例如正式、口語或其他短字串。 |
| Keywords | 提供人名、專有名詞或活動術語給修正與翻譯流程使用。 |
| Pinned keywords | 鎖定特定 keyword，避免被自動排序或淘汰。 |
| Text dictionary | 在修正與翻譯前做直接文字替換。 |
| Co-owners | 主要擁有者可新增協作者。 |
| Microphone | 開啟或關閉即時音訊輸入。 |

## 7. API 與即時通訊

### 7.1 觀眾端 SSE

觀眾頁使用 SSE：

```text
GET /api/session/{session_id}/stream
```

事件名稱：

```text
transcription_update
```

用途：

- `/rt/{session_id}` 接收即時字幕。
- `/yt/{session_id}` 接收即時字幕。
- 支援 `Last-Event-ID` 或 `last_event_id` 續接。

### 7.2 Panel Socket.IO 事件

Panel 使用 Socket.IO 做雙向控制。

Client to server：

| Event | 用途 |
|---|---|
| `join_session` | 使用 `session_id` 與 `secret_key` 加入 session。 |
| `sync` | 送出外部轉錄資料。 |
| `realtime_connect` | 初始化即時轉錄管理器。 |
| `mic_on` | 啟動即時轉錄。 |
| `mic_off` | 停止即時轉錄。 |
| `audio_buffer_append` | 傳送 base64 音訊片段。 |
| `leave_session` | 離開 session room。 |

Server to client：

| Event | 用途 |
|---|---|
| `connected` | Socket.IO 連線完成。 |
| `joined_session` | 已加入 session，回傳 viewer count。 |
| `transcription_update` | Panel 端收到字幕更新。 |
| `viewer_count_update` | 觀眾數更新。 |
| `error` | 驗證、限流或資料格式錯誤。 |

### 7.3 Session 設定 API

以下 API 需要 session 管理權限：

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/session/{sid}/languages` | 讀取翻譯語言。 |
| `POST` | `/api/session/{sid}/languages` | 更新翻譯語言。 |
| `GET` | `/api/session/{sid}/keywords` | 讀取 keywords 與 locked keywords。 |
| `POST` | `/api/session/{sid}/keywords` | 更新 keywords 與 locked keywords。 |
| `GET` | `/api/session/{sid}/text-dictionary` | 讀取文字字典。 |
| `POST` | `/api/session/{sid}/text-dictionary` | 更新文字字典。 |
| `GET` | `/api/session/{sid}/scribe-language` | 讀取 Scribe 語言設定。 |
| `POST` | `/api/session/{sid}/scribe-language` | 更新 Scribe 語言設定。 |
| `GET` | `/api/session/{sid}/translate-tone` | 讀取翻譯語氣。 |
| `POST` | `/api/session/{sid}/translate-tone` | 更新翻譯語氣。 |
| `GET` | `/api/session/{sid}/co-owners` | 讀取 session 協作者。 |
| `POST` | `/api/session/{sid}/co-owners` | 新增 co-owner。 |
| `DELETE` | `/api/session/{sid}/co-owners/{email}` | 移除 co-owner。 |
| `PUT` | `/api/session/{sid}/segments` | 更新已儲存字幕片段。 |
| `DELETE` | `/api/session/{sid}/segments` | 刪除已儲存字幕片段。 |

### 7.4 管理員 API

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/api/users/{email}/realtime` | 開啟或關閉指定使用者的即時轉錄權限。 |

## 8. 資料儲存

| 儲存位置 | 用途 |
|---|---|
| MongoDB `rooms` | session 擁有者、secret key、co-owner、設定與使用量。 |
| MongoDB `transcription_segments` | 已完成的字幕片段。 |
| MongoDB `transcription_store` | session metadata 與舊資料相容。 |
| Redis `transcription:{sid}:list` | 近期已完成字幕片段快取。 |
| Redis `transcription:{sid}:partial` | 尚未完成的 partial 字幕。 |
| Redis `transcription:{sid}:meta` | 串流開始時間等 metadata。 |
| Redis `keywords:{sid}` | session keywords 快取。 |
| Redis `locked_keywords:{sid}` | pinned keywords 快取。 |
| Redis `text_dictionary:{sid}` | 文字字典快取。 |

## 9. 階段成果紀錄

| 時間 | Commit | 對應項目 | 說明 |
|---|---|---|---|
| 2025-11-22 | 0637440 | 即時轉錄研究 | 新增 `realtime_client`，包含 ElevenLabs 與 Google STT 即時轉錄測試程式。 |
| 2025-11-22 | 7eb521a | Web 版雛形 | 重整為 `live_server`、`transcribe_client`、`realtime_client`，新增網頁模板與服務架構。 |
| 2025-11-26 | 6f50a9e | 使用者建立 session | 使用者可建立 session，系統產生 secret key，新增 panel 頁面。 |
| 2025-12-22 | 0ad34f2 | WebSocket / Docker / DB 基礎 | 改寫為 FastAPI、Redis、MongoDB，新增 `Dockerfile` 與 `docker-compose.yml`。 |
| 2026-02-24 | cfb50c4 | 即時轉錄與翻譯主流程 | 新增 Scribe manager、即時前端 JS、翻譯模組與 Docker 設定。 |
| 2026-03-06 | f9bc8ff | 專業字詞 / 關鍵字 | 新增 session keywords API 與 panel 編輯介面。 |
| 2026-03-10 | c12f3a7 | 使用者帳號系統 | 新增 email OTP 登入、管理後台與即時轉錄權限管理。 |
| 2026-04-09 | b536b0f | 自動關鍵字 | 將關鍵字改為頻率字典，加入抽取與排序邏輯。 |
| 2026-04-16 | e1be265 | 字幕資料儲存 | 將轉錄資料移到獨立 segments collection，改善管理與查詢基礎。 |
| 2026-04-21 | 32cd088 | 歷史紀錄管理 | 新增字幕編輯頁與 session segments 管理 API。 |
| 2026-04-23 | 25f77ef | 匯出功能 | 新增單語言 SRT 匯出。 |
| 2026-05-06 | a698ad2 | 使用者自訂字詞庫 | 新增 text dictionary API 與 panel UI，翻譯流程會套用使用者定義替換。 |
| 2026-05-16 | 9154e4b | 觀眾端傳輸調整 | 觀眾端即時通訊改為 SSE，並新增 viewer count。 |

## 10. 成果確認清單

### 10.1 啟動檢查

- `live_server/app/config.py` 已存在。
- MongoDB 可連線。
- Redis 可連線。
- 至少一組 AI provider API key 已設定。
- 若使用即時麥克風，`ELEVENLABS_API_KEY` 已設定。
- 伺服器可開啟 `/`。
- `/login` 可完成 OTP 登入。

### 10.2 權限檢查

- 管理員可進入 `/dashboard`。
- 管理員可切換使用者 realtime 權限。
- 一般使用者可進入 `/user-dashboard`。
- Session 擁有者可進入 `/panel/{session_id}`。
- 未授權使用者不可進入他人的 panel。
- Co-owner 可進入已授權 session 的 panel。

### 10.3 即時字幕檢查

- Panel 可加入 Socket.IO session。
- 開啟麥克風後會建立 Scribe session。
- `audio_buffer_append` 可持續送出音訊。
- Panel 可看到 corrected transcription。
- `/rt/{session_id}` 可收到 SSE 更新。
- `/yt/{session_id}` 可收到 SSE 更新。
- viewer count 會更新。

### 10.4 字幕資料檢查

- 完成片段會寫入 MongoDB。
- Redis 有近期字幕快取。
- `/edit/{session_id}` 可讀取已儲存片段。
- 修改字幕後重新整理頁面仍可看到修改結果。
- 刪除字幕片段後不再出現在編輯頁與匯出檔。

### 10.5 匯出檢查

- `/download/{session_id}` 可輸出 JSON。
- `/download/{session_id}/srt/{lang}` 可輸出 SRT。
- SRT 時間軸從第一段字幕開始計算。
- 指定不存在的語言時會回傳 404。

## 11. 常見問題

### 11.1 無法登入

檢查項目：

- `EMAIL_SETTINGS.SMTP_HOST` 是否設定正確。
- 若未設定 SMTP，確認開發環境是否從 log 取得 OTP。
- Redis 是否正常，OTP 需要 Redis 儲存暫存碼。

### 11.2 使用者看不到麥克風功能

檢查項目：

- 使用者是否已登入。
- 管理員是否已在 `/dashboard` 開啟該 email 的 realtime 權限。
- 瀏覽器是否允許麥克風權限。
- 網頁是否透過可使用麥克風的安全來源開啟，例如 `localhost` 或 HTTPS。

### 11.3 觀眾頁沒有字幕

檢查項目：

- Session ID 是否一致。
- Panel 是否已成功加入 session。
- 麥克風是否已開啟。
- Redis 是否正常。
- 瀏覽器 Network 是否有連上 `/api/session/{session_id}/stream`。

### 11.4 有轉錄但沒有翻譯

檢查項目：

- `REALTIME_SETTINGS.AI_PROVIDER` 是否設定為可用 provider。
- 對應 API key 是否存在且有效。
- `TRANSLATE_LANGUAGES` 是否有目標語言。
- Provider quota 是否用完。
- 伺服器 log 是否有翻譯 API 錯誤。

### 11.5 YouTube 頁時間軸不準

檢查項目：

- `SETTINGS.YOUTUBE_API_KEY` 是否設定。
- YouTube video ID 是否正確。
- 該影片是否有 `actualStartTime` 或 `scheduledStartTime`。
- 若 YouTube API 無法取得時間，頁面仍可顯示字幕，但同步基準可能需要人工確認。

## 12. 已知限制

- 即時轉錄流程目前依賴外部 STT 與 AI provider，延遲與穩定性會受 provider 狀態影響。
- 觀眾端使用 SSE，適合一對多廣播；需要雙向互動的功能應留在 panel Socket.IO。
- SRT 匯出以已儲存的 committed segments 為準，不包含仍在處理中的 partial segment。
- `/edit/{session_id}` 修改後，已開啟的觀眾頁可能需要重新整理才會看到歷史片段修正。
- Text dictionary 是直接替換，設定時應避免過短或容易誤傷的字串。
