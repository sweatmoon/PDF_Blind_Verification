FROM python:3.12-slim

# 시스템 패키지 (PyMuPDF, Tesseract OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 의존성 먼저 설치 (캐시 활용)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 복사
COPY backend/ ./backend/
COPY frontend/ ./frontend/
# data 디렉토리 (dictionary.json 등 기본 설정 포함)
COPY data/ ./data/

# 런타임 디렉토리 생성
RUN mkdir -p /app/tmp /app/logs /app/data/reports

WORKDIR /app/backend

# Railway는 PORT 환경변수를 동적으로 주입 (기본 8080)
ENV PORT=8080
EXPOSE ${PORT}

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 300
