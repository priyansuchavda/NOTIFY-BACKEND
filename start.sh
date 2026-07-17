#!/usr/bin/env bash
set -o errexit

# Free-tier safe: no Render Shell required.
python manage.py migrate --noinput
python manage.py ensure_superuser
exec gunicorn notifi_backend.wsgi:application --bind 0.0.0.0:${PORT:-8000}
