#!/bin/sh
set -eu
skill_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -x "$skill_dir/.venv/bin/python3" ]; then
  echo '运行环境未准备好；先运行本 skill 的 scripts/setup.sh。' >&2
  exit 2
fi
exec "$skill_dir/.venv/bin/python3" "$skill_dir/scripts/workflow.py" "$@"
