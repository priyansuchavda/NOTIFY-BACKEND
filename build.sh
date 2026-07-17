#!/usr/bin/env bash
set -o errexit

echo "==> Python version"
python --version
python -c "import sys; assert sys.version_info[:2] == (3, 11), f'Need Python 3.11, got {sys.version}'; print('Python 3.11 OK')"

echo "==> Installing dependencies"
pip install -r requirements.txt

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Running database migrations"
python manage.py migrate --noinput

echo "==> Build complete"
