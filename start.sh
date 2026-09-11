#!/bin/sh
# Render startup — use gunicorn in production
exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4
