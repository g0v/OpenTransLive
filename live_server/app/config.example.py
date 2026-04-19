SETTINGS = {
  "YOUTUBE_API_KEY": "your-youtube-api-key",
  "SECRET_KEY": "your-secret-key"
}

EMAIL_SETTINGS = {
  # List of email addresses with admin dashboard access
  "ADMIN_EMAILS": ["admin@example.com"],

  # SMTP configuration for sending OTP emails
  # Leave SMTP_HOST empty to disable email sending (OTP will be logged instead)
  "SMTP_HOST": "",
  "SMTP_PORT": 587,
  "SMTP_USERNAME": "",
  "SMTP_PASSWORD": "",
  "SMTP_FROM": "noreply@example.com",
  "SMTP_USE_TLS": True,
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

  # AI provider API keys (Correction and translation)
  'GEMINI_API_KEY': "",
  'OPENAI_API_KEY': "",
  'GROQ_API_KEY': "",
  'CEREBRAS_API_KEY': "",

  # AI provider default: "gemini", "openai", "groq", or "cerebras"
  # Override per-operation with CORRECT_PROVIDER / TRANSLATE_PROVIDER.
  # If both are unset they fall back to AI_PROVIDER.
  'AI_PROVIDER': "openai",
  # 'CORRECT_PROVIDER':   "gemini",   # provider used for ASR correction
  # 'TRANSLATE_PROVIDER': "groq",     # provider used for translation

  # Translation settings
  'TRANSLATE_LANGUAGES': "zh-Hant-TW,en-US",
  'COMMON_PROMPT': "",

  # Partial transcription flush interval in seconds
  'PARTIAL_INTERVAL': 1,
  'SKIP_CORRECTION': False
}
