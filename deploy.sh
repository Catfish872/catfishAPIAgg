#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

SERVICE_CONTAINER="catfishapiagg_service"
DATA_DIR="./data"
DEFAULT_IMAGE="ghcr.io/catfish872/catfishapiagg:latest"
TEMP_CONTAINER="catfishapiagg_data_copy_once"

mkdir -p "$DATA_DIR"

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

get_compose_image() {
  image=""
  if docker compose version >/dev/null 2>&1; then
    image=$(docker compose config --images 2>/dev/null | head -n 1 || true)
  else
    image=$(docker-compose config 2>/dev/null | awk '/image:/ {print $2; exit}' || true)
  fi
  if [ -z "$image" ]; then
    image="$DEFAULT_IMAGE"
  fi
  echo "$image"
}

is_empty_or_missing_config() {
  if [ ! -f "$DATA_DIR/config.json" ]; then
    return 0
  fi

  compact=$(tr -d '[:space:]' < "$DATA_DIR/config.json" 2>/dev/null || echo "")
  if [ "$compact" = "{}" ] || [ "$compact" = "[]" ] || [ -z "$compact" ]; then
    return 0
  fi

  return 1
}

has_complete_data() {
  if [ -f "$DATA_DIR/config.json" ] && [ -f "$DATA_DIR/stats.json" ] && ! is_empty_or_missing_config; then
    return 0
  fi
  return 1
}

copy_from_container() {
  source_container="$1"
  if docker ps -a --format '{{.Names}}' | grep -qx "$source_container"; then
    echo "[catfishAPIAgg] Trying data from container: $source_container"
    docker cp "$source_container:/app/data/." "$DATA_DIR/" 2>/dev/null || true
  fi
}

copy_from_volume() {
  volume_name="$1"
  image_name="$2"

  echo "[catfishAPIAgg] Trying data from Docker volume: $volume_name"
  docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true

  if docker create --name "$TEMP_CONTAINER" -v "$volume_name:/app/data:ro" "$image_name" >/dev/null 2>&1; then
    docker cp "$TEMP_CONTAINER:/app/data/." "$DATA_DIR/" 2>/dev/null || true
    docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true
  else
    echo "[catfishAPIAgg] Cannot create temp container with image $image_name, skip volume $volume_name"
    docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true
  fi
}

migrate_if_needed() {
  if has_complete_data; then
    echo "[catfishAPIAgg] ./data already has valid config.json and stats.json, skip migration."
    return
  fi

  echo "[catfishAPIAgg] ./data is missing data or config.json is empty, trying migration..."

  image_name=$(get_compose_image)

  # 第一优先级：旧容器。适合还没被新 compose 重建过的用户。
  copy_from_container "$SERVICE_CONTAINER"

  if has_complete_data; then
    echo "[catfishAPIAgg] Migration succeeded from old container."
    return
  fi

  # 第二优先级：旧命名卷。适合容器已经被新 compose 切到 ./data，但旧卷还在的用户。
  volume_names=$(docker volume ls --format '{{.Name}}' | grep -E '(^|_)catfish_data$|catfishapiagg.*catfish_data|catfish.*data' || true)

  for volume_name in $volume_names; do
    copy_from_volume "$volume_name" "$image_name"
    if has_complete_data; then
      echo "[catfishAPIAgg] Migration succeeded from Docker volume: $volume_name"
      return
    fi
  done

  echo "[catfishAPIAgg] No legacy data found. The app will create fresh data files if needed."
}

migrate_if_needed

compose_cmd up -d

echo "[catfishAPIAgg] Done. Data directory: $(pwd)/data"
