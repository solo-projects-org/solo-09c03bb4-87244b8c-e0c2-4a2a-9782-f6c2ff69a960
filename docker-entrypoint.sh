#!/bin/sh
# 容器入口：启动前如库为空可载入样例，随后用 gunicorn 起服务。
set -e

if [ "${LOAD_SAMPLE_ON_EMPTY:-0}" = "1" ]; then
  python -m app.bootstrap
fi

exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 30 \
  "app.webapp:create_app()"
