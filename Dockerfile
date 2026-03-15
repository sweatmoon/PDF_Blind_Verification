FROM python:3.12-slim

# 시스템 패키지 (PyMuPDF, Tesseract OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 의존성 먼저 설치 (캐시 활용)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 복사
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY start.sh ./start.sh

# 런타임 디렉토리 생성 + 실행 권한
# data/는 이미지에 포함하지 않음 - Railway Volume 마운트 대상
# 단, logo_reference.png는 이미지에 포함 (로고 탐지 필수)
RUN mkdir -p /app/tmp /app/logs /app/data/reports \
    && chmod +x /app/start.sh
COPY data/logo_reference.png /app/data/logo_reference.png

ENV PORT=8080
ENV TZ=Asia/Seoul
EXPOSE 8080

WORKDIR /app/backend

ENTRYPOINT ["/app/start.sh"]
