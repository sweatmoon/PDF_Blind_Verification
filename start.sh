#!/bin/sh
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1 --timeout-keep-alive 300
