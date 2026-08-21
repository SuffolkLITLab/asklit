#!/bin/sh
set -eu

mkdir -p /app/data
if [ ! -f /app/data/app.sqlite3 ]; then
  cp -a /opt/asklit-seed-data/. /app/data/
fi

exec streamlit run app.py \
  --server.port=8501 \
  --server.address=0.0.0.0 \
  --server.headless=true
