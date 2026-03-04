SETTINGS = {
  "YOUTUBE_API_KEY": "your-youtube-api-key",
  "SECRET_KEY": "your-secret-key"
}

MONGODB_SETTINGS = {
    'db': 'opentranslive-db',
    'host': 'mongodb',
    'port': 27017
}

REDIS_URL = "redis://redis:6379"

REALTIME_SETTINGS = {
  # ElevenLabs Scribe (speech-to-text)
  'ELEVENLABS_API_KEY': "",

  # AI translation provider: "gemini" or "openai"
  'AI_PROVIDER': "gemini",
  'GEMINI_API_KEY': "",
  'OPENAI_API_KEY': "",
  'AI_MODEL': "gemini-3.1-flash-lite-preview",  # or e.g. "gpt-4.1-mini" for openai

  # Translation settings
  'TRANSLATE_LANGUAGES': "zh-Hant,ja,ko,en",
  'COMMON_PROMPT': "",

  # Partial transcription flush interval in seconds
  'PARTIAL_INTERVAL': 2,
}
