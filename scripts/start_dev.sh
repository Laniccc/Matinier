#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BACKEND_ROOT="$PROJECT_ROOT/backend"
FRONTEND_ROOT="$PROJECT_ROOT/frontend"
ENV_FILE="$PROJECT_ROOT/.env"

CHECK_ONLY=false
SKIP_MIGRATION=false
for argument in "$@"; do
  case "$argument" in
    --check-only) CHECK_ONLY=true ;;
    --skip-migration) SKIP_MIGRATION=true ;;
    *)
      printf 'Unknown argument: %s\n' "$argument" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  printf 'Missing %s. Copy .env.example to .env and configure it.\n' "$ENV_FILE" >&2
  exit 1
fi

if [[ -x "$BACKEND_ROOT/.venv/bin/python" ]]; then
  PYTHON="$BACKEND_ROOT/.venv/bin/python"
elif [[ -x "$BACKEND_ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON="$BACKEND_ROOT/.venv/Scripts/python.exe"
else
  printf 'Missing backend Python environment. Run uv sync in backend/.\n' >&2
  exit 1
fi

PNPM="$(command -v pnpm || true)"
if [[ -z "$PNPM" ]]; then
  printf 'pnpm was not found on PATH.\n' >&2
  exit 1
fi
if [[ ! -d "$FRONTEND_ROOT/node_modules" ]]; then
  printf 'Missing frontend/node_modules. Run pnpm install in frontend/.\n' >&2
  exit 1
fi

(cd "$BACKEND_ROOT" && "$PYTHON" -c "from app.settings import Settings; s=Settings(); print('Configuration: ok'); print('Database: ' + s.database_location)")
PERSISTENT_LOG_ROOT="$(cd "$BACKEND_ROOT" && "$PYTHON" -c "from app.settings import Settings; print(Settings().logs_dir)")"

printf 'FastAPI: %s -m uvicorn app.main:app --host 127.0.0.1 --port 8000\n' "$PYTHON"
printf 'Worker: %s -m app.worker.entrypoint dev\n' "$PYTHON"
printf 'Frontend: %s dev --hostname 127.0.0.1 --port 3000\n' "$PNPM"
printf 'Replay: %s %s/scripts/run_demo_replay.py --file <audio.wav> --session-id <session-id>\n' "$PYTHON" "$PROJECT_ROOT"

if [[ "$CHECK_ONLY" == true ]]; then
  printf 'Check-only passed. No migration or process was started.\n'
  exit 0
fi

if [[ "$SKIP_MIGRATION" == false ]]; then
  (cd "$BACKEND_ROOT" && "$PYTHON" -m alembic upgrade head)
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_ROOT="$PERSISTENT_LOG_ROOT/dev-$TIMESTAMP"
# Per-process files: api.stdout.log, worker.stdout.log, frontend.stdout.log
mkdir -p "$LOG_ROOT"
PID_FILE="$LOG_ROOT/processes.json"
PIDS=()
NAMES=()
USE_PROCESS_GROUPS=false
if command -v setsid >/dev/null 2>&1; then
  USE_PROCESS_GROUPS=true
fi

start_child() {
  local name="$1"
  local working_directory="$2"
  shift 2
  if [[ "$USE_PROCESS_GROUPS" == true ]]; then
    (cd "$working_directory" && exec setsid "$@") \
      >"$LOG_ROOT/$name.stdout.log" \
      2>"$LOG_ROOT/$name.stderr.log" &
  else
    (cd "$working_directory" && exec "$@") \
      >"$LOG_ROOT/$name.stdout.log" \
      2>"$LOG_ROOT/$name.stderr.log" &
  fi
  local process_id=$!
  PIDS+=("$process_id")
  NAMES+=("$name")
  printf '%s started PID=%s\n' "$name" "$process_id"
}

cleanup() {
  local index
  for ((index=${#PIDS[@]}-1; index>=0; index--)); do
    local process_id="${PIDS[$index]}"
    if kill -0 "$process_id" 2>/dev/null; then
      if [[ "$USE_PROCESS_GROUPS" == true ]]; then
        kill -TERM -- "-$process_id" 2>/dev/null || true
      else
        kill -TERM "$process_id" 2>/dev/null || true
      fi
    fi
  done
  wait "${PIDS[@]}" 2>/dev/null || true
  printf 'Stopped recorded development processes. Logs remain at %s\n' "$LOG_ROOT"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

start_child api "$BACKEND_ROOT" \
  "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
start_child worker "$BACKEND_ROOT" \
  "$PYTHON" -m app.worker.entrypoint dev
start_child frontend "$FRONTEND_ROOT" \
  "$PNPM" dev --hostname 127.0.0.1 --port 3000

printf '[\n' >"$PID_FILE"
for index in "${!PIDS[@]}"; do
  separator=","
  if [[ "$index" -eq $((${#PIDS[@]} - 1)) ]]; then
    separator=""
  fi
  printf '  {"name":"%s","pid":%s,"stdout":"%s.stdout.log","stderr":"%s.stderr.log"}%s\n' \
    "${NAMES[$index]}" "${PIDS[$index]}" "${NAMES[$index]}" "${NAMES[$index]}" "$separator" \
    >>"$PID_FILE"
done
printf ']\n' >>"$PID_FILE"

printf 'Logs: %s\n' "$LOG_ROOT"
printf 'Press Ctrl+C to stop the recorded API, Worker and Frontend trees.\n'
wait -n "${PIDS[@]}"
