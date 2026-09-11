#!/bin/sh
# Works on both Render (sets $PORT) and Fly.io (sets $PORT)
exec gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4
