# OpenTransLive 階段成果紀錄

| 時間 | Commit | 對應項目 | 說明 |
|---|---|---|---|
| 2025-11-22 | `0637440` try realtime client | 即時轉錄研究 | 新增 `realtime_client`，包含 ElevenLabs / Google STT 即時轉錄測試程式。 |
| 2025-11-22 | `7eb521a` restructure | Web 版雛形 | 重整為 `live_server`、`transcribe_client`、`realtime_client`，新增網頁模板與服務架構。 |
| 2025-11-26 | `6f50a9e` let anyone can create session and gen secret_key | 使用者建立 session | 使用者可建立 session，系統產生 secret key，新增 panel 頁面。 |
| 2025-12-22 | `0ad34f2` rewrite with fastAPI + redis + mongodb | WebSocket / Docker / DB 基礎 | 改寫為 FastAPI + Redis + MongoDB，新增 `Dockerfile` 與 `docker-compose.yml`。 |
| 2026-02-24 | `cfb50c4` feat: Implement initial real-time transcription and translation system with server, client, and Docker setup | 即時轉錄與翻譯主流程 | 新增 Scribe manager、即時前端 JS、翻譯模組與 Docker 設定。 |
| 2026-03-10 | `c12f3a7` Add email OTP login and admin dashboard for realtime permission management | 使用者帳號系統 | 新增 email OTP 登入、管理後台與即時轉錄權限管理。 |
| 2026-04-16 | `e1be265` refactor: migrate transcription storage to dedicated segments collection | 字幕資料儲存 | 將轉錄資料移到獨立 segments collection，改善後續管理與查詢基礎。 |
| 2026-04-21 | `32cd088` feat: add transcription editor page and API endpoints for managing session segments | 歷史紀錄管理 | 新增字幕編輯頁與 session segments 管理 API。 |
| 2026-04-23 | `25f77ef` feat: add SRT export functionality for individual languages in session transcriptions | 匯出功能 | 新增單語言 SRT 匯出。 |
| 2026-03-06 | `f9bc8ff` feat: add session keywords view and edit via API and panel UI | 專業字詞 / 關鍵字 | 新增 session keywords API 與 panel 編輯介面。 |
| 2026-04-09 | `b536b0f` refactor: replace keyword list with frequency-based dictionary and update re-ranking logic to use keyword extraction and scoring | 自動關鍵字 | 將關鍵字改為頻率字典，加入抽取與排序邏輯。 |
| 2026-05-06 | `a698ad2` feat: add text dictionary feature | 使用者自訂字詞庫 | 新增 text dictionary API、panel UI，翻譯流程會套用使用者定義替換。 |
| 2026-05-16 | `9154e4b` refactor: migrate real-time communication from Socket.IO to Server-Sent Events (SSE) and add viewer count display | 觀眾端傳輸調整 | 觀眾端即時通訊改為 SSE，並新增 viewer count。 |
