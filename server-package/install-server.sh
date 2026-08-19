#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 运行此安装脚本。" >&2
  exit 1
fi

PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/cws-codex"
VENV_DIR="/opt/cws-codex-venv"
CONFIG_DIR="/etc/cws-codex"
STATE_DIR="/var/lib/cws-codex"

command -v python3 >/dev/null 2>&1 || {
  echo "未找到 python3，请先安装 Python 3.10 或更高版本及 python3-venv。" >&2
  exit 1
}
command -v systemctl >/dev/null 2>&1 || {
  echo "未找到 systemctl，本安装包只支持 systemd。" >&2
  exit 1
}

install -d -m 0755 "${APP_DIR}"
install -d -m 0700 "${CONFIG_DIR}" "${STATE_DIR}" "${STATE_DIR}/accounts"
install -m 0755 "${PACKAGE_DIR}/codex_plus_broker.py" "${APP_DIR}/codex_plus_broker.py"
install -m 0755 "${PACKAGE_DIR}/codex_device_tokens.py" "${APP_DIR}/codex_device_tokens.py"
install -m 0755 "${PACKAGE_DIR}/import_employee_tokens_xlsx.py" "${APP_DIR}/import_employee_tokens_xlsx.py"
install -m 0644 "${PACKAGE_DIR}/codex_quota_dashboard.html" "${APP_DIR}/codex_quota_dashboard.html"
install -m 0644 "${PACKAGE_DIR}/requirements-server.txt" "${APP_DIR}/requirements-server.txt"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${APP_DIR}/requirements-server.txt"

if [[ ! -f "${CONFIG_DIR}/broker.env" ]]; then
  install -m 0600 "${PACKAGE_DIR}/cws-codex-broker.env.example" "${CONFIG_DIR}/broker.env"
  echo "已创建 ${CONFIG_DIR}/broker.env；启动前必须替换管理员令牌。"
else
  echo "保留现有 ${CONFIG_DIR}/broker.env。"
fi

install -m 0644 "${PACKAGE_DIR}/cws-codex-broker.service" "/etc/systemd/system/cws-codex-broker.service"
systemctl daemon-reload

echo
echo "服务端程序安装完成，尚未自动启动。"
echo "请阅读 ${PACKAGE_DIR}/README-Server.txt，配置 auth.json 和管理员令牌后执行："
echo "  systemctl enable --now cws-codex-broker"
