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
  # Gemini or OpenAI (Correction and translate)
  'GEMINI_API_KEY': "",
  'OPENAI_API_KEY': "",

  # AI translation provider: "gemini" or "openai"
  'AI_PROVIDER': "gemini",
  'AI_MODEL': "gemini-3.1-flash-lite-preview",  # or e.g. "gpt-4.1-mini" for openai

  # Translation settings
  'TRANSLATE_LANGUAGES': "zh-Hant-TW,en-US",
  'COMMON_PROMPT': "",

  # Partial transcription flush interval in seconds
  'PARTIAL_INTERVAL': 2
}
