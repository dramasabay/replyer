FROM python:3.12-slim

WORKDIR /app

# sqlite3 CLI is handy for debugging inside the container; not required at runtime.
# gosu is used in entrypoint.sh to drop privileges to appuser safely.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# app.db and secret.key live under DATA_DIR (set via docker-compose env),
# which is volume-mounted so they survive container rebuilds/restarts.
# entrypoint.sh seeds that volume from data_seed/ on first boot only.
ENV DATA_DIR=/app/data

# Create non-root user appuser (UID 1000). entrypoint.sh runs initially as root
# to ensure DATA_DIR and any mounted volume ownership are correct, then drops
# privileges to appuser before launching the application.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

VOLUME ["/app/data"]

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
