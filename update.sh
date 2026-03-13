#!/bin/bash
# 코드 업데이트 후 무중단 재배포
set -e
GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }

info "최신 코드 가져오는 중..."
git pull origin main

info "컨테이너 재빌드 및 재시작..."
docker compose build app
docker compose up -d --no-deps app

info "✅ 업데이트 완료!"
docker compose ps
