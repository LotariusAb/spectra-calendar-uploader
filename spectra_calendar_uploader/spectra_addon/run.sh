#!/usr/bin/env sh
set -e

cd /app

export PYTHONUNBUFFERED=1

# Gunicorn startet als Package: app.server:app
# - gthread: parallel Upload + CPU Work
# - timeout 180s: Dithering + ESP Upload kann dauern
exec gunicorn \
  --bind 0.0.0.0:8088 \
  --worker-class gthread \
  --threads 4 \
  --timeout 180 \
  "app.server:app"
