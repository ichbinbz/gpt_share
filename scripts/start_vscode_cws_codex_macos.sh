#!/bin/zsh
set -eu

SCRIPT_DIR=${0:A:h}
CWS_CODEX_HOME=${CWS_CODEX_HOME:-"$HOME/.cws-codex"}
VSCODE_BIN=${VSCODE_BIN:-"/Applications/Visual Studio Code.app/Contents/MacOS/Code"}

if [[ ! -x "$VSCODE_BIN" ]]; then
  print -u2 "Visual Studio Code executable not found: $VSCODE_BIN"
  exit 1
fi

if pgrep -x "Code" >/dev/null 2>&1; then
  print -u2 "Please fully quit every VS Code window before using this launcher."
  exit 1
fi

python3 "$SCRIPT_DIR/codex_plus_sync.py" --codex-home "$CWS_CODEX_HOME"
export CODEX_HOME="$CWS_CODEX_HOME"
exec "$VSCODE_BIN" "$@"
