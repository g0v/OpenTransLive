# OpenTransLive 階段成果

本文件只記錄專案的階段性成果。實際使用方式、API、部署請看 [docs/USAGE.md](docs/USAGE.md)。

語言：繁體中文（[English](milestone.en.md)）

| 時間 | Commit | 對應項目 | 說明 |
|---|---|---|---|
| 2025-11-22 | 0637440 | 即時轉錄研究 | 新增 `realtime_client`，包含 ElevenLabs 與 Google STT 即時轉錄測試程式 |
| 2025-11-22 | 7eb521a | Web 版雛形 | 重整為 `live_server`、`transcribe_client`、`realtime_client`，新增網頁模板與服務架構 |
| 2025-11-26 | 6f50a9e | 使用者建立 session | 使用者可建立 session，系統產生 secret key，新增 panel 頁面 |
| 2025-12-22 | 0ad34f2 | WebSocket / Docker / DB 基礎 | 改寫為 FastAPI、Redis、MongoDB，新增 `Dockerfile` 與 `docker-compose.yml` |
| 2026-02-24 | cfb50c4 | 即時轉錄與翻譯主流程 | 新增 Scribe manager、即時前端 JS、翻譯模組與 Docker 設定 |
| 2026-03-06 | f9bc8ff | 專業字詞 / 關鍵字 | 新增 session keywords API 與 panel 編輯介面 |
| 2026-03-10 | c12f3a7 | 使用者帳號系統 | 新增 email OTP 登入、管理後台與即時轉錄權限管理 |
| 2026-04-09 | b536b0f | 自動關鍵字 | 將關鍵字改為頻率字典，加入抽取與排序邏輯 |
| 2026-04-16 | e1be265 | 字幕資料儲存 | 將轉錄資料移到獨立 segments collection，改善管理與查詢基礎 |
| 2026-04-21 | 32cd088 | 歷史紀錄管理 | 新增字幕編輯頁與 session segments 管理 API |
| 2026-04-23 | 25f77ef | 匯出功能 | 新增單語言 SRT 匯出 |
| 2026-05-06 | a698ad2 | 使用者自訂字詞庫 | 新增 text dictionary API 與 panel UI，翻譯流程會套用使用者定義替換 |
| 2026-05-16 | 9154e4b | 觀眾端傳輸調整 | 觀眾端即時通訊改為 SSE，並新增 viewer count |
