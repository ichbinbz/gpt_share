#!/usr/bin/env bash
set -euo pipefail

VERSION="0.1.2"
BROKER_URL="http://codex.cws.internal:8765"
PROXY_URL="http://192.168.2.38:7897"
CODEX_HOME="${HOME}/.cws-codex"
SKIP_EXTENSION=0
SKIP_SYNC=0
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/cws-codex"
BIN_DIR="${HOME}/.local/bin"
APPLICATION_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
LOG_PATH="${TMPDIR:-/tmp}/CWS-Codex-Linux-Install.log"

trap 'status=$?; if (( status != 0 )); then printf "%s 安装失败（退出码 %s）\n" "$(date --iso-8601=seconds)" "$status" > "$LOG_PATH"; printf "安装失败，日志：%s\n" "$LOG_PATH" >&2; fi' EXIT

while (( $# )); do
    case "$1" in
        --broker-url) BROKER_URL="$2"; shift 2 ;;
        --proxy-url) PROXY_URL="$2"; shift 2 ;;
        --direct) PROXY_URL=""; shift ;;
        --codex-home) CODEX_HOME="$2"; shift 2 ;;
        --skip-extension) SKIP_EXTENSION=1; shift ;;
        --skip-sync) SKIP_SYNC=1; shift ;;
        -h|--help)
            echo "用法：./install.sh [--broker-url URL] [--proxy-url URL|--direct] [--codex-home PATH] [--skip-extension] [--skip-sync]"
            exit 0
            ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

if [[ "${EUID}" -eq 0 ]]; then
    echo "请以普通桌面用户运行，不要使用 sudo。" >&2
    exit 1
fi
command -v python3 >/dev/null || { echo "缺少 python3。" >&2; exit 1; }

VSCODE_PATH="$(command -v code 2>/dev/null || true)"
if [[ -z "$VSCODE_PATH" && -x /snap/bin/code ]]; then VSCODE_PATH=/snap/bin/code; fi
if [[ -z "$VSCODE_PATH" && -x /usr/bin/code ]]; then VSCODE_PATH=/usr/bin/code; fi
if [[ -z "$VSCODE_PATH" ]]; then
    echo "未找到 VS Code，请先安装 Visual Studio Code。" >&2
    exit 1
fi

umask 077
install -d -m 700 "$INSTALL_DIR" "$BIN_DIR" "$APPLICATION_DIR" "$SYSTEMD_DIR" "$CODEX_HOME"
install -m 755 "$SCRIPT_DIR/cws_codex.py" "$INSTALL_DIR/cws_codex.py"
install -m 755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
install -m 644 "$SCRIPT_DIR/README-Linux.txt" "$INSTALL_DIR/README-Linux.txt"
ln -sfn "$INSTALL_DIR/cws_codex.py" "$BIN_DIR/cws-codex"

python3 "$INSTALL_DIR/cws_codex.py" setup \
    --broker-url "$BROKER_URL" \
    --proxy-url "$PROXY_URL" \
    --codex-home "$CODEX_HOME" \
    --vscode-path "$VSCODE_PATH"

cat > "$APPLICATION_DIR/cws-codex.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=公司 Codex（VS Code）
Comment=使用公司 Codex 凭据启动 Visual Studio Code
Exec=$INSTALL_DIR/cws_codex.py launch
Icon=com.visualstudio.code
Terminal=false
Categories=Development;IDE;
StartupNotify=true
StartupWMClass=Code
EOF
chmod 600 "$APPLICATION_DIR/cws-codex.desktop"

if [[ -d "$HOME/Desktop" ]]; then
    cp "$APPLICATION_DIR/cws-codex.desktop" "$HOME/Desktop/公司 Codex.desktop"
    chmod 755 "$HOME/Desktop/公司 Codex.desktop"
    command -v gio >/dev/null && gio set "$HOME/Desktop/公司 Codex.desktop" metadata::trusted true >/dev/null 2>&1 || true
fi

cat > "$SYSTEMD_DIR/cws-codex-sync.service" <<EOF
[Unit]
Description=CWS Codex credential synchronization and usage report

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 "$INSTALL_DIR/cws_codex.py" background
EOF

cat > "$SYSTEMD_DIR/cws-codex-sync.timer" <<'EOF'
[Unit]
Description=Run CWS Codex background synchronization every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
chmod 600 "$SYSTEMD_DIR/cws-codex-sync.service" "$SYSTEMD_DIR/cws-codex-sync.timer"

if command -v systemctl >/dev/null && systemctl --user daemon-reload >/dev/null 2>&1; then
    systemctl --user enable --now cws-codex-sync.timer >/dev/null
else
    echo "警告：当前会话无法启用 systemd 用户定时器，可稍后运行：systemctl --user enable --now cws-codex-sync.timer" >&2
fi

if (( SKIP_EXTENSION == 0 )); then
    "$VSCODE_PATH" --install-extension openai.chatgpt --force || echo "警告：Codex 扩展自动安装失败，请在 VS Code 中手动安装。" >&2
fi
if (( SKIP_SYNC == 0 )); then
    python3 "$INSTALL_DIR/cws_codex.py" sync --new-lease || echo "警告：首次同步失败，连接代理后运行 cws-codex diagnose。" >&2
fi

rm -f -- "$LOG_PATH"
echo "CWS Codex Linux 客户端 v${VERSION} 安装完成。"
echo "从应用程序菜单或桌面的“公司 Codex（VS Code）”启动。"
echo "命令行诊断：cws-codex diagnose"
