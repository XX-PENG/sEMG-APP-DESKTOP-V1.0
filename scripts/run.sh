#!/usr/bin/env bash
# macOS / Linux 启动脚本：首次运行自动建 .venv 并装依赖
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

py="$root/.venv/bin/python"
if command -v uv >/dev/null 2>&1; then
  [ -x "$py" ] || uv venv .venv
else
  [ -x "$py" ] || python3 -m venv .venv
fi

if ! "$py" -c "import semg_desktop, PySide6, pyqtgraph, numpy, scipy, serial, bleak" >/dev/null 2>&1; then
  echo "安装依赖（可编辑安装 + dev）"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$py" -e ".[dev]"
  else
    "$py" -m pip install -e ".[dev]"
  fi
fi

if [ "$#" -eq 0 ]; then
  set -- gui --simulate
fi
exec "$py" -m semg_desktop "$@"
