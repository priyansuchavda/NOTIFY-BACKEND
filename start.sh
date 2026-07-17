#!/usr/bin/env bash
set -o errexit

echo "==> Running database migrations"
python manage.py migrate --noinput

echo "==> Ensuring admin superuser exists"
python manage.py ensure_superuser

echo "==> Starting gunicorn"
exec gunicorn notifi_backend.wsgi:application --bind 0.0.0.0:${PORT:-8000}
