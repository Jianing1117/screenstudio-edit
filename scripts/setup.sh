#!/bin/sh
set -eu
skill_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -x "$skill_dir/.venv/bin/python3" ]; then
  runtime_python=${SCREENSTUDIO_PYTHON:-$(command -v python3 || true)}
  if [ -z "$runtime_python" ] || ! "$runtime_python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo '需要 Python 3.11 或更新版本（例如 brew install python），也可用 SCREENSTUDIO_PYTHON 指定。' >&2
    exit 2
  fi
  "$runtime_python" -m venv "$skill_dir/.venv"
fi
"$skill_dir/.venv/bin/python3" -m pip install --disable-pip-version-check -r "$skill_dir/scripts/requirements.txt"
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || { echo '还需要 ffmpeg（例如 brew install ffmpeg）。' >&2; exit 2; }
"$skill_dir/.venv/bin/python3" -c 'import numpy, onnxruntime; print("Screen Studio 运行环境就绪")'
