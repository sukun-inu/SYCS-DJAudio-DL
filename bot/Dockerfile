FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py cache.py config.py isrc_meta.py site_detection.py ./

RUN mkdir -p /app/data /app/cache

CMD ["python", "-u", "bot.py"]
