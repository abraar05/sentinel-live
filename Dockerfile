# Sentinel Live gateway.
#
# The DVRIP client needs a stable outbound TCP connection to each NVR, so the
# container keeps one outbound connection per device open at a time and must
# not be aggressively auto-stopped - see fly.toml.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    SENTINEL_BIND=0.0.0.0 \
    SENTINEL_DATA=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Recordings and the encrypted device store live here. Mount a volume, or they
# vanish with the container.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["sh", "-c", "exec python3 -m uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8080}"]