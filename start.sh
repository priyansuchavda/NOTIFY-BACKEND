#!/usr/bin/env bash
set -o errexit

# Free-tier safe: no Render Shell required.
# Migrate on boot so admin/auth tables always exist.
python manage.py migrate --noinput
exec gunicorn notifi_backend.wsgi:application --bind 0.0.0.0:${PORT:-8000}
