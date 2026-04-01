#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ -z "$1" ]; then
  echo "Usage: ./run.sh <repo_url> [branch]"
  exit 1
fi

BRANCH=${2:-main}
python app/main.py "$1" --branch "$BRANCH"
