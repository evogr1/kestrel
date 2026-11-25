# Kestrel server for claude.ai custom connectors and the REST API.
#   docker build -t kestrel .
#   docker run -d --name kestrel -p 127.0.0.1:8799:8799 --env-file .env -v kestrel-data:/data kestrel
# Put it behind HTTPS (Cloudflare Tunnel, Caddy or similar) before adding it to claude.ai.
FROM python:3.12-slim
ARG TRANSCRIBE=0
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir yt-dlp cryptography \
 && if [ "$TRANSCRIBE" = "1" ]; then pip install --no-cache-dir faster-whisper; fi
WORKDIR /app
COPY *.py ./
RUN useradd -m -d /data kestrel && mkdir -p /data && chown kestrel /data
USER kestrel
ENV HOME=/data SCOUT_DB=/data/.scout/cache.sqlite SCOUT_HOST=0.0.0.0 SCOUT_PORT=8799 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8799
CMD ["python3", "scout_server.py"]
