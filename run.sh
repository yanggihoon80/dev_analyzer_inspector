#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ -z "$1" ]; then
  echo "사용법: ./run.sh <repo_url> [branch]"
  exit 1
fi

BRANCH=${2:-main}
if command -v python >/dev/null 2>&1; then
  PYTHON_CMD=python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=python3
else
  echo "Python 실행 파일을 찾을 수 없습니다. Python을 설치한 뒤 다시 시도하세요."
  exit 1
fi

"$PYTHON_CMD" app/main.py "$1" --branch "$BRANCH"
