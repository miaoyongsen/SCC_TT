#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/miaoyongsen/SCC_TT.git"
INSTALL_DIR="${SCC_TT_DIR:-$HOME/SCC_TT}"
PID_DIR="$INSTALL_DIR/run"
LOG_DIR="$INSTALL_DIR/logs"
PID_FILE="$PID_DIR/uvicorn.pid"
APP_LOG="$LOG_DIR/nohup.log"
PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"
APP_MODULE="app.main:app"

log() { printf '[SCC_TT] %s\n' "$*"; }
die() { printf '[SCC_TT] ERROR: %s\n' "$*" >&2; exit 1; }

ensure_repo() {
  command -v git >/dev/null 2>&1 || die "未安装 git，请先安装 git。"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -c http.version=HTTP/1.1 -C "$INSTALL_DIR" pull --ff-only
  elif [[ -e "$INSTALL_DIR" ]]; then
    if [[ -d "$INSTALL_DIR" ]] && [[ -z "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      rmdir "$INSTALL_DIR"
    else
      die "$INSTALL_DIR 已存在但不是 Git 仓库且目录非空，请检查目录或设置 SCC_TT_DIR。"
    fi
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git -c http.version=HTTP/1.1 clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  else
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git -c http.version=HTTP/1.1 clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
}

ensure_env() {
  cd "$INSTALL_DIR"
  [[ -f .env ]] || cp .env.example .env

  # Generate a private random token on first deployment; never commit .env.
  if grep -q '^WEBHOOK_TOKEN=CHANGE_THIS_TO_A_LONG_RANDOM_SECRET$' .env; then
    "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import re, secrets
p = Path(".env")
s = p.read_text(encoding="utf-8")
s = re.sub(r"(?m)^WEBHOOK_TOKEN=.*$", "WEBHOOK_TOKEN=" + secrets.token_urlsafe(48), s, count=1)
p.write_text(s, encoding="utf-8")
PY
    chmod 600 .env
    log "已自动生成 WEBHOOK_TOKEN，保存在 $INSTALL_DIR/.env"
  fi
}

load_env() {
  cd "$INSTALL_DIR"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  export HOST="${HOST:-0.0.0.0}"
  export PORT="${PORT:-8081}"
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -Fq "uvicorn $APP_MODULE"
}

install_deps() {
  command -v python3 >/dev/null 2>&1 || die "未找到 python3。"
  cd "$INSTALL_DIR"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    python3 -m venv .venv || die "创建虚拟环境失败；Debian/Ubuntu 可安装 python3-venv。"
  fi
  "$PYTHON_BIN" -m pip install --upgrade "pip<25"
  "$PYTHON_BIN" -m pip install -r requirements.txt
  mkdir -p "$PID_DIR" "$LOG_DIR" "$INSTALL_DIR/data"
}

start_service() {
  cd "$INSTALL_DIR"
  [[ -x "$PYTHON_BIN" ]] || die "尚未安装依赖，请先执行 install。"
  [[ -f .env ]] || die "缺少 .env，请先执行 install。"
  load_env
  mkdir -p "$PID_DIR" "$LOG_DIR" "$INSTALL_DIR/data"

  if is_running; then
    log "服务已经运行，PID=$(cat "$PID_FILE")"
    return 0
  fi
  rm -f "$PID_FILE"

  nohup "$PYTHON_BIN" -m uvicorn "$APP_MODULE" \
    --host "$HOST" --port "$PORT" --workers 1 \
    >> "$APP_LOG" 2>&1 < /dev/null &
  local pid=$!
  echo "$pid" > "$PID_FILE"

  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      tail -n 60 "$APP_LOG" >&2 || true
      die "服务启动失败，请检查日志：$APP_LOG"
    fi
    if "$PYTHON_BIN" -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$PORT"; then
      log "启动成功，PID=$pid，监听端口=$PORT"
      log "日志：$APP_LOG"
      return 0
    fi
    sleep 1
  done

  log "进程已启动，但健康检查暂未成功；请查看日志：$APP_LOG"
}

stop_service() {
  if ! is_running; then
    rm -f "$PID_FILE"
    log "服务当前未运行。"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid"
  for _ in $(seq 1 15); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      log "服务已停止。"
      return 0
    fi
    sleep 1
  done
  log "等待超时，发送 SIGKILL。"
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
}

usage() {
  cat <<EOF
用法：bash deploy.sh [install|start|stop|restart|update|status|logs]
  install  拉取代码、创建虚拟环境、安装依赖、生成配置并启动（默认）
  start    启动服务
  stop     停止服务
  restart  重启服务
  update   拉取最新代码、更新依赖并重启
  status   查看运行状态
  logs     实时查看日志
可通过 SCC_TT_DIR=/自定义路径 指定安装目录（默认：$HOME/SCC_TT）
EOF
}

ACTION="${1:-install}"
case "$ACTION" in
  install)
    ensure_repo
    install_deps
    ensure_env
    start_service
    ;;
  start)
    [[ -d "$INSTALL_DIR/.git" ]] || die "未找到项目，请先执行 install。"
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  update)
    ensure_repo
    install_deps
    ensure_env
    stop_service
    start_service
    ;;
  status)
    if is_running; then
      log "运行中，PID=$(cat "$PID_FILE")"
    else
      log "未运行"
      exit 1
    fi
    ;;
  logs)
    mkdir -p "$LOG_DIR"
    touch "$APP_LOG"
    tail -n 100 -f "$APP_LOG"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    die "未知操作：$ACTION"
    ;;
esac
