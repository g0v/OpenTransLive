# OpenTransLive Usage Guide

This document describes the roles, flows, URLs, APIs, and data storage used by OpenTransLive. It focuses on concrete behavior rather than aspirational descriptions.

Language: English ([繁體中文](USAGE.md))

---

## 1. Roles

| Role | Login required | Purpose |
|---|---:|---|
| System admin | Yes | Manage which users may use realtime transcription |
| Session owner | Yes | Create and manage sessions, open the panel, configure languages, keywords, co-owners |
| Session co-owner | Yes | Enter the panel of an authorized session and adjust settings |
| Viewer | No | Watch subtitles via `/rt/{session_id}` or `/yt/{session_id}` |

## 2. Core Flows

### 2.1 Create and manage a session

1. Open `/login`.
2. Enter email and complete OTP verification.
3. Regular users land at `/user-dashboard`.
4. Create or open an existing session.
5. Enter `/panel/{session_id}` to control the session.

- The first logged-in user to create a session becomes the primary owner.
- The primary owner may add co-owners.
- If the panel management lock times out, the owner or a co-owner can reclaim control.

### 2.2 Start realtime transcription

1. The owner or a co-owner opens `/panel/{session_id}`.
2. Confirm that the account is allowed to use realtime transcription (granted by the admin).
3. In the panel, configure: target translation languages, Scribe detection language, translation tone, keywords, and the text dictionary.
4. Turn on the microphone.
5. The server forwards audio to the realtime transcription service, then runs correction and translation.
6. Completed segments are written to Redis and MongoDB.
7. Viewers receive updates over SSE.

### 2.3 Viewer subtitles

- Realtime subtitle page: `/rt/{session_id}`
- YouTube-synced subtitle page: `/yt/{session_id}`

Viewer behavior:

- No login required.
- Uses Server-Sent Events to receive subtitle updates.
- The page UI offers language and layout switching.
- `/yt/{session_id}` works with YouTube live or recorded videos.

### 2.4 Edit historical subtitles

The owner or a co-owner can open `/edit/{session_id}` to:

- Edit the corrected text of stored segments.
- Edit the translation of each language.
- Delete unwanted segments.
- Edits update MongoDB; Redis caches are updated in sync if present.

### 2.5 Export subtitles

- Full JSON export: `/download/{session_id}`
- Single-language SRT export: `/download/{session_id}/srt/{lang}`

Examples:

```text
/download/demo-session/srt/zh-Hant-TW
/download/demo-session/srt/en-US
```

Constraints:

- If the session has an owner, export requires owner or co-owner permission.
- SRT export only includes segments that have a translation in the requested language.

## 3. URLs

| Path | Purpose | Access |
|---|---|---|
| `/` | Landing page | Public |
| `/login` | Email OTP login | Public |
| `/logout` | Log out | Authenticated |
| `/dashboard` | Admin console | System admin |
| `/user-dashboard` | User session list | Authenticated |
| `/panel/{session_id}` | Session control panel | Owner or co-owner |
| `/rt/{session_id}` | Realtime subtitle view | Public |
| `/yt/{session_id}` | YouTube subtitle view | Public |
| `/edit/{session_id}` | Historical subtitle editor | Owner or co-owner |
| `/download/{session_id}` | JSON export | Per session policy |
| `/download/{session_id}/srt/{lang}` | SRT export | Per session policy |

## 4. Panel Settings

| Item | Description |
|---|---|
| Translation languages | Languages the subtitles are translated into |
| Scribe language | STT language; blank means auto-detect |
| Translate tone | Short string for translation tone, e.g. formal, casual |
| Keywords | Names, terminology, or event-specific terms used by correction and translation |
| Pinned keywords | Lock specific keywords against auto-rotation or eviction |
| Text dictionary | Direct text substitution before correction and translation |
| Co-owners | The primary owner may add collaborators |
| Microphone | Toggle realtime audio input |

## 5. APIs and Realtime Transport

### 5.1 Viewer SSE

The viewer pages use SSE:

```text
GET /api/session/{session_id}/stream
```

Event name: `transcription_update`

Used by:

- `/rt/{session_id}`
- `/yt/{session_id}`
- Resumable via `Last-Event-ID` or `last_event_id`.

### 5.2 Panel Socket.IO Events

The panel uses Socket.IO for bidirectional control.

Client to server:

| Event | Purpose |
|---|---|
| `join_session` | Join a session with `session_id` and `secret_key` |
| `sync` | Submit externally generated transcription data |
| `realtime_connect` | Initialize the realtime transcription manager |
| `mic_on` | Start realtime transcription |
| `mic_off` | Stop realtime transcription |
| `audio_buffer_append` | Send a base64-encoded audio chunk |
| `leave_session` | Leave the session room |

Server to client:

| Event | Purpose |
|---|---|
| `connected` | Socket.IO connection established |
| `joined_session` | Joined session; includes viewer count |
| `transcription_update` | Subtitle update for the panel |
| `viewer_count_update` | Viewer count changed |
| `error` | Auth, rate limit, or schema error |

### 5.3 Session Settings API

Requires session management permission.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/session/{sid}/languages` | Read translation languages |
| `POST` | `/api/session/{sid}/languages` | Update translation languages |
| `GET` | `/api/session/{sid}/keywords` | Read keywords and locked keywords |
| `POST` | `/api/session/{sid}/keywords` | Update keywords and locked keywords |
| `GET` | `/api/session/{sid}/text-dictionary` | Read text dictionary |
| `POST` | `/api/session/{sid}/text-dictionary` | Update text dictionary |
| `GET` | `/api/session/{sid}/scribe-language` | Read Scribe language |
| `POST` | `/api/session/{sid}/scribe-language` | Update Scribe language |
| `GET` | `/api/session/{sid}/translate-tone` | Read translation tone |
| `POST` | `/api/session/{sid}/translate-tone` | Update translation tone |
| `GET` | `/api/session/{sid}/co-owners` | Read co-owners |
| `POST` | `/api/session/{sid}/co-owners` | Add a co-owner |
| `DELETE` | `/api/session/{sid}/co-owners/{email}` | Remove a co-owner |
| `PUT` | `/api/session/{sid}/segments` | Update stored subtitle segments |
| `DELETE` | `/api/session/{sid}/segments` | Delete stored subtitle segments |

### 5.4 Admin API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/users/{email}/realtime` | Toggle realtime transcription permission for a user |

## 6. Data Storage

| Location | Purpose |
|---|---|
| MongoDB `rooms` | Session owner, secret key, co-owners, settings, usage |
| MongoDB `transcription_segments` | Committed subtitle segments |
| MongoDB `transcription_store` | Session metadata; legacy compatibility |
| Redis `transcription:{sid}:list` | Recent committed segment cache |
| Redis `transcription:{sid}:partial` | In-flight partial subtitle |
| Redis `transcription:{sid}:meta` | Stream start time and metadata |
| Redis `keywords:{sid}` | Session keywords cache |
| Redis `locked_keywords:{sid}` | Pinned keywords cache |
| Redis `text_dictionary:{sid}` | Text dictionary cache |

## 7. Acceptance Checks

### 7.1 Startup

- `live_server/app/secret/config.toml` exists (copied from `secret/config.example.toml`).
- MongoDB is reachable.
- Redis is reachable.
- At least one AI provider API key is set.
- For realtime mic: `ELEVENLABS_API_KEY` is set.
- The server serves `/`.
- `/login` completes OTP login.

### 7.2 Permissions

- Admin can enter `/dashboard`.
- Admin can toggle a user's realtime permission.
- Regular users can enter `/user-dashboard`.
- Session owner can enter `/panel/{session_id}`.
- Unauthorized users cannot enter someone else's panel.
- Co-owners can enter the panel of an authorized session.

### 7.3 Realtime Subtitles

- The panel can join the Socket.IO session.
- Turning on the mic establishes a Scribe session.
- `audio_buffer_append` keeps streaming audio.
- The panel sees corrected transcriptions.
- `/rt/{session_id}` receives SSE updates.
- `/yt/{session_id}` receives SSE updates.
- Viewer count updates.

### 7.4 Subtitle Data

- Committed segments land in MongoDB.
- Redis holds the recent cache.
- `/edit/{session_id}` loads stored segments.
- Edits persist across reloads.
- Deleted segments disappear from the editor and exports.

### 7.5 Export

- `/download/{session_id}` returns JSON.
- `/download/{session_id}/srt/{lang}` returns SRT.
- SRT timing starts at the first segment.
- Unknown languages return 404.

## 8. Troubleshooting

### 8.1 Cannot log in

- Verify `EMAIL_SETTINGS.SMTP_HOST`.
- Without SMTP, check whether dev logs print the OTP.
- Ensure Redis is healthy (OTP storage).

### 8.2 No microphone UI

- Confirm the user is logged in.
- Confirm the admin granted realtime permission via `/dashboard`.
- Browser must allow microphone access.
- Page must be served from a secure origin (`localhost` or HTTPS).

### 8.3 Viewer page has no subtitles

- Session ID matches.
- Panel successfully joined the session.
- Mic is on.
- Redis is healthy.
- Browser network panel shows a live `/api/session/{session_id}/stream`.

### 8.4 Transcription exists but no translation

- `REALTIME_SETTINGS.AI_PROVIDER` is set to an available provider.
- Provider API key is present and valid.
- `TRANSLATE_LANGUAGES` lists target languages.
- Provider quota is not exhausted.
- Check server logs for translation API errors.

### 8.5 YouTube timeline is off

- `SETTINGS.YOUTUBE_API_KEY` is set.
- The YouTube video ID is correct.
- The video has `actualStartTime` or `scheduledStartTime`.
- If the YouTube API cannot provide a start time, subtitles still appear but the sync baseline may need manual confirmation.

## 9. Known Limitations

- Realtime transcription depends on external STT and AI providers; latency and stability follow provider health.
- Viewer transport is SSE — fit for one-to-many broadcast; bidirectional features should stay in the panel Socket.IO channel.
- SRT export reflects committed segments only and excludes in-flight partials.
- After edits via `/edit/{session_id}`, already-open viewer pages may need a reload to see historical changes.
- The text dictionary is a direct substitution; avoid strings that are too short or easily collide.
