#!/usr/bin/env bash
# AGENT://BREAK — запуск. После сна песочницы просто запусти снова: bash run.sh
cd "$(dirname "$0")"
mkdir -p data
exec python3 server.py --host 0.0.0.0 --port "${PORT:-8000}"
