FROM python:3.12-slim

WORKDIR /app

# sqlite3 CLI is handy for debugging inside the container; not required at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# app.db and secret.key live under DATA_DIR (set via docker-compose env),
# which is volume-mounted so they survive container rebuilds/restarts.
# entrypoint.sh seeds that volume from data_seed/ on first boot only.
ENV DATA_DIR=/app/data

# Run as a non-root user rather than the container default (root). The
# chown happens before VOLUME is declared, so Docker carries that same
# ownership over when it initializes the named volume on first run.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

VOLUME ["/app/data"]

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
