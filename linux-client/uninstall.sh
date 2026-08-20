#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/cws-codex"
BIN_PATH="${HOME}/.local/bin/cws-codex"
APPLICATION_PATH="${XDG_DATA_HOME:-${HOME}/.local/share}/applications/cws-codex.desktop"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"

if command -v systemctl >/dev/null; then
    systemctl --user disable --now cws-codex-sync.timer >/dev/null 2>&1 || true
fi
rm -f -- "$SYSTEMD_DIR/cws-codex-sync.timer" "$SYSTEMD_DIR/cws-codex-sync.service"
command -v systemctl >/dev/null && systemctl --user daemon-reload >/dev/null 2>&1 || true
rm -f -- "$APPLICATION_PATH" "$HOME/Desktop/公司 Codex.desktop"
if [[ -L "$BIN_PATH" && "$(readlink -f -- "$BIN_PATH")" == "$INSTALL_DIR/cws_codex.py" ]]; then
    rm -f -- "$BIN_PATH"
fi

answer="${1:-}"
if [[ "$answer" != "--remove-credentials" ]]; then
    read -r -p "是否同时删除 ~/.cws-codex 中的短期凭据？[y/N]：" answer
fi
if [[ "$answer" == "--remove-credentials" || "$answer" == [yY]* ]]; then
    rm -rf -- "$HOME/.cws-codex"
fi

expected="${XDG_DATA_HOME:-${HOME}/.local/share}/cws-codex"
if [[ "$INSTALL_DIR" != "$expected" || "$INSTALL_DIR" == "/" || -z "$INSTALL_DIR" ]]; then
    echo "拒绝删除异常安装目录：$INSTALL_DIR" >&2
    exit 1
fi
rm -rf -- "$INSTALL_DIR"
echo "CWS Codex Linux 客户端已卸载。"
