#!/usr/bin/env bash
# ============================================================
#  start.sh — QQ Bot v3 桌面一键启动（Ubuntu 桌面版）
#
#  用法:
#    ./start.sh                启动 GUI（前台；自动建 venv/装依赖，首次较慢）
#    ./start.sh install        在应用菜单/桌面生成快捷方式「QQ Bot」
#    ./start.sh remove         移除快捷方式
#    ./start.sh status         查看 bot / GUI 运行状态
#    ./start.sh stop           停止 bot（GUI 关闭即可，bot 会跟随退出）
#
#  说明:
#    - 依赖装在项目内 .venv（Ubuntu 24.04+ 系统 pip 受 PEP 668 保护，
#      统一走 venv，不污染系统 Python）
#    - 桌面快捷方式以 --bg 后台模式运行 GUI；终端直接跑则为前台模式，
#      关闭窗口即退出（bot 由 GUI 内确认框决定是否保留后台）
#    - bot 本体由 GUI 拉起（总览页「启动 bot」或自动），端口 8696/8697
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
REQ="$ROOT/requirements.txt"
RUNDIR="$ROOT/data/run"
BG_PIDFILE="$RUNDIR/gui.bg.pid"
DESKTOP_FILE="$HOME/.local/share/applications/qq-bot.desktop"
DESKTOP_LINK="$HOME/Desktop/qq-bot.desktop"
APPS="QQ Bot"
MIN_PY="3.10"

c_green() { printf '\033[32m%s\033[0m' "$1"; }
c_red()   { printf '\033[31m%s\033[0m' "$1"; }
c_dim()   { printf '\033[2m%s\033[0m' "$1"; }

# ------------------------------------------------------------
#  ① 系统 Python 检查
# ------------------------------------------------------------
need_apt_hint=0
ensure_system_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ 未找到 python3。Ubuntu 安装: sudo apt update && sudo apt install -y python3" >&2
    exit 1
  fi
  local v
  v=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
  if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PY/./,}) else 1)"; then
    echo "✗ 需要 Python ${MIN_PY}+，当前 $v。" >&2
    echo "  Ubuntu 24.04 默认 3.12 满足；旧系统可装 deadsnakes PPA 后重试。" >&2
    exit 1
  fi
  # venv 模块（Ubuntu 需 python3-venv 包）
  if ! python3 -c 'import venv' 2>/dev/null; then
    echo "✗ 缺少 venv 模块。Ubuntu 安装: sudo apt install -y python3-venv" >&2
    exit 1
  fi
}

# ------------------------------------------------------------
#  ② 虚拟环境 + 依赖
# ------------------------------------------------------------
ensure_venv() {
  if [[ ! -x "$PY" ]]; then
    echo "[venv] 创建虚拟环境 $VENV ..."
    python3 -m venv "$VENV" || {
      echo "✗ venv 创建失败。如提示 ensurepip 缺失: sudo apt install -y python3-venv" >&2
      exit 1
    }
  fi
  # 依赖是否齐（任一缺失则重装，幂等）
  if ! "$PY" -c "import websockets, aiohttp, httpx, requests, PIL, pypinyin, yaml, jieba, PySide6" 2>/dev/null; then
    echo "[pip] 安装依赖（首次约 1-3 分钟，PySide6 约 500MB 下载）..."
    "$PY" -m pip install --quiet --upgrade pip || true
    if ! "$PY" -m pip install --quiet -r "$REQ"; then
      echo "✗ 依赖安装失败（网络问题？可手动重试: $PY -m pip install -r $REQ）" >&2
      exit 1
    fi
  fi
}

# ------------------------------------------------------------
#  ③ Qt 运行库检查（PySide6 需要系统 Qt 库）
# ------------------------------------------------------------
ensure_qt_libs() {
  if [[ ! -f "$PY" ]]; then return 0; fi
  # 无图形会话（无 DISPLAY）：GUI 起不来，提前给指引
  if [[ -z "${DISPLAY:-}" ]] && [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo "⚠ 当前没有图形会话（DISPLAY 未设置），GUI 无法显示窗口。" >&2
    echo "  请在 Ubuntu 桌面环境下运行本脚本（登录桌面后打开终端/应用菜单）。" >&2
    echo "  无头服务器场景请使用 VNC/RDP 连接桌面后运行。" >&2
    return 1
  fi
  if "$PY" -c "import os; os.environ.setdefault('QT_QPA_PLATFORM','offscreen'); from PySide6.QtWidgets import QApplication; a=QApplication([])" 2>/dev/null; then
    return 0
  fi
  echo "⚠ PySide6 无法初始化，多半缺 Qt 系统库。Ubuntu 安装:" >&2
  echo "    sudo apt install -y libgl1 libegl1 libxcb-cursor0 libxkbcommon0 libdbus-1-3 libfontconfig1 libglib2.0-0" >&2
  echo "  安装后重新运行 ./start.sh" >&2
  exit 1
}

# ------------------------------------------------------------
#  ④ 桌面快捷方式
# ------------------------------------------------------------
do_install_desktop() {
  local script_abs
  script_abs="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  mkdir -p "$(dirname "$DESKTOP_FILE")"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=${APPS}
Comment=QQ 群机器人控制台（GUI）
Exec="${script_abs}" --bg
Icon=$ROOT/assets/icon.png
Terminal=false
Categories=Utility;Network;
StartupNotify=true
EOF
  # 桌面链接（桌面若开启"受信任"策略则显示，否则在应用菜单）
  if [[ -d "$HOME/Desktop" ]]; then
    cp "$DESKTOP_FILE" "$DESKTOP_LINK" 2>/dev/null || true
    chmod +x "$DESKTOP_LINK" 2>/dev/null || true
    # GNOME 默认要求桌面图标标记为受信任才显示，尽力而为
    if command -v gio >/dev/null 2>&1; then
      gio set "$DESKTOP_LINK" metadata::trusted true 2>/dev/null || true
    fi
  fi
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
  echo "$(c_green '✓ 快捷方式已安装')  应用菜单搜索「${APPS}」或桌面双击图标启动。"
  echo "  (移除: $0 remove)"
}

do_remove_desktop() {
  rm -f "$DESKTOP_FILE" "$DESKTOP_LINK"
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
  echo "✓ 快捷方式已移除"
}

# ------------------------------------------------------------
#  ⑤ 启动 GUI
#     前台: exec 替换当前 shell（Ctrl+C / 关窗口即退）
#     后台: nohup + pidfile（桌面快捷方式用，关终端不影响）
# ------------------------------------------------------------
do_launch() {
  mkdir -p "$RUNDIR"
  ensure_system_python
  ensure_venv
  ensure_qt_libs || exit 1

  local mode="${1:-fg}"
  if [[ "$mode" == "bg" ]]; then
    # 已在跑则直接提示
    if [[ -f "$BG_PIDFILE" ]] && kill -0 "$(cat "$BG_PIDFILE" 2>/dev/null)" 2>/dev/null; then
      echo "$(c_dim 'GUI 已在后台运行 (pid '$(cat "$BG_PIDFILE")')，直接打开窗口即可。')"
      return 0
    fi
    nohup "$PY" "$ROOT/gui_launcher.py" --start \
        >> "$RUNDIR/gui.out" 2>&1 < /dev/null &
    local pid=$!
    echo "$pid" > "$BG_PIDFILE"
    sleep 3
    if kill -0 "$pid" 2>/dev/null; then
      echo "$(c_green '✓ GUI 已后台启动 (pid '$pid')')"
      echo "  日志: $RUNDIR/gui.out   状态: $0 status"
    else
      echo "$(c_red '✗ GUI 启动失败，日志:')"; tail -20 "$RUNDIR/gui.out" >&2
      rm -f "$BG_PIDFILE"
      return 1
    fi
  else
    echo "$(c_dim '前台模式: 关窗口退出（bot 是否保留后台由 GUI 确认框决定）')"
    exec "$PY" "$ROOT/gui_launcher.py" --start
  fi
}

# ------------------------------------------------------------
#  ⑥ 状态
# ------------------------------------------------------------
do_status() {
  local out=""
  # bot：控制 API 8697
  local st
  st=$(curl -s --max-time 2 http://127.0.0.1:8697/status 2>/dev/null)
  if [[ -n "$st" ]]; then
    local nap
    nap=$(echo "$st" | "$ROOT/.venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); print(d["napcat"]["connected"])' 2>/dev/null || echo "?")
    echo "  bot:   运行中 (pid $(echo "$st" | "$ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["pid"])' 2>/dev/null || echo '?'))  NapCat 已连接=$nap"
    out="bot_running"
  else
    echo "  bot:   未运行（GUI 总览页点「启动 bot」）"
  fi
  # GUI：pidfile 或进程匹配
  if [[ -f "$BG_PIDFILE" ]] && kill -0 "$(cat "$BG_PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "  GUI:   后台运行 (pid $(cat "$BG_PIDFILE"))"
  elif ps -eo args 2>/dev/null | grep -q "[g]ui_launcher.py"; then
    echo "  GUI:   运行中 (前台)"
  else
    echo "  GUI:   未运行（./start.sh 启动）"
  fi
  # 端口
  local ports
  ports=$(ss -tln 2>/dev/null | awk '{print $4}' | grep -oE ':(8696|8697)$' | sort -u | tr '\n' ' ')
  [[ -n "$ports" ]] && echo "  端口:  $ports 监听中"
}

# ------------------------------------------------------------
#  ⑦ 停止 bot（优雅退出，走控制 API）
# ------------------------------------------------------------
do_stop() {
  if curl -s --max-time 2 -X POST http://127.0.0.1:8697/restart >/dev/null 2>&1; then
    sleep 2
    if curl -s --max-time 2 http://127.0.0.1:8697/status >/dev/null 2>&1; then
      echo "⚠ bot 未退出（可能 GUI 又拉起了），可关 GUI 时选「停止 bot」"
    else
      echo "✓ bot 已停止（GUI 可保留运行）"
    fi
  else
    echo "bot 未在运行"
  fi
}

# ------------------------------------------------------------
cmd="${1:-start}"
case "$cmd" in
  start)   do_launch fg ;;
  install) do_install_desktop ;;
  remove)  do_remove_desktop ;;
  status)  do_status ;;
  stop)    do_stop ;;
  --bg)    do_launch bg ;;
  *) echo "用法: $0 {start|install|remove|status|stop}" >&2; exit 1 ;;
esac
