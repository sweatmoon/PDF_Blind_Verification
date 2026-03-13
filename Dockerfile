FROM python:3.12-slim

# 시스템 패키지 (PyMuPDF, Tesseract OCR)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 의존성 먼저 설치 (캐시 활용)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 복사
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 런타임 디렉토리 생성
RUN mkdir -p /app/tmp /app/logs /app/data/reports

WORKDIR /app/backend

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000", \
     "--workers", "1", "--timeout-keep-alive", "300"]
