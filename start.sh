#!/bin/sh

# Volume 마운트 후 필수 파일 복원 (logo_reference.png)
# Railway Volume이 /app/data/ 에 마운트되면 이미지 내 파일이 가려질 수 있으므로
# 빌드 시 복사해둔 백업에서 복원
LOGO_DEST="/app/data/logo_reference.png"
LOGO_BACKUP="/app/logo_reference.png.bak"

if [ ! -f "$LOGO_DEST" ] && [ -f "$LOGO_BACKUP" ]; then
    cp "$LOGO_BACKUP" "$LOGO_DEST"
    echo "[start.sh] logo_reference.png 복원 완료"
fi

mkdir -p /app/data/reports /app/tmp /app/logs

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1 --timeout-keep-alive 300
