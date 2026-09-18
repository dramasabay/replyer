#!/bin/sh
set -e

# On first container start the mounted volume at DATA_DIR is empty. If you
# drop a real secret.key/app.db into backend/data_seed/ before building (e.g.
# migrating an existing deployment), they get copied in on that first boot.
# data_seed/ ships empty by default — nothing here, so a fresh key and DB
# are created automatically. On every later start the volume already has
# real data, so we never overwrite it.
mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/secret.key" ] && [ -f /app/data_seed/secret.key ]; then
    cp /app/data_seed/secret.key "$DATA_DIR/secret.key"
    chmod 600 "$DATA_DIR/secret.key"
fi

if [ ! -f "$DATA_DIR/app.db" ] && [ -f /app/data_seed/app.db ]; then
    cp /app/data_seed/app.db "$DATA_DIR/app.db"
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000
