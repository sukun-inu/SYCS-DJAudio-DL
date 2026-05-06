import os
from pathlib import Path

CACHE_DIR        = Path(os.getenv("CACHE_DIR", "/app/cache"))
CACHE_TTL        = int(os.getenv("CACHE_TTL_SECONDS", "600"))
BASE_URL         = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "30"))
MAX_URLS_PER_MSG = int(os.getenv("MAX_URLS_PER_MSG", "3"))
DL_CONCURRENCY   = int(os.getenv("DL_CONCURRENCY", "3"))
DL_TIMEOUT       = int(os.getenv("DL_TIMEOUT_SECONDS", "120"))
FFMPEG_PATH      = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
