#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

SERVICE_CONTAINER="catfishapiagg_service"
DATA_DIR="./data"

mkdir -p "$DATA_DIR"

need_migrate=0
if [ ! -f "$DATA_DIR/config.json" ] || [ ! -f "$DATA_DIR/stats.json" ]; then
  need_migrate=1
fi

if [ "$need_migrate" = "1" ]; then
  echo "[catfishAPIAgg] ./data is missing config.json or stats.json, trying migration..."

  if docker ps -a --format '{{.Names}}' | grep -qx "$SERVICE_CONTAINER"; then
    echo "[catfishAPIAgg] Found old container, copying /app/data to ./data..."
    docker cp "$SERVICE_CONTAINER:/app/data/." "$DATA_DIR/" 2>/dev/null || true
  fi

  if [ ! -f "$DATA_DIR/config.json" ] || [ ! -f "$DATA_DIR/stats.json" ]; then
    VOLUME_NAMES=$(docker volume ls --format '{{.Name}}' | grep -E '(^|_)catfish_data$|catfishapiagg.*catfish_data' || true)
    for volume_name in $VOLUME_NAMES; do
      echo "[catfishAPIAgg] Trying old Docker volume: $volume_name"
      docker run --rm \
        -v "$volume_name:/old:ro" \
        -v "$(pwd)/data:/new" \
        busybox sh -c 'cp -an /old/. /new/ 2>/dev/null || true'
    done
  fi
else
  echo "[catfishAPIAgg] ./data already has config.json and stats.json, skip migration."
fi

if docker compose version >/dev/null 2>&1; then
  docker compose up -d
else
  docker-compose up -d
fi

echo "[catfishAPIAgg] Done. Data directory: $(pwd)/data"
