#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAKENSIS_BIN="${MAKENSIS_BIN:-makensis}"

command -v "${MAKENSIS_BIN}" >/dev/null 2>&1 || {
  echo "未找到 makensis。macOS 可执行：brew install nsis" >&2
  exit 1
}

"${MAKENSIS_BIN}" -V2 -DPROJECT_ROOT="${PROJECT_ROOT}" "${PROJECT_ROOT}/windows-installer/CwsCodex.nsi"
