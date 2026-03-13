#!/bin/bash
# ============================================================
# 입찰 제안서 블라인드 검증 서비스 - 서버 배포 스크립트
# 사용법: bash deploy.sh
# ============================================================
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 1. 환경 확인 ───────────────────────────────────────────
info "시스템 확인 중..."
command -v docker        >/dev/null 2>&1 || error "Docker가 설치되어 있지 않습니다. https://docs.docker.com/engine/install/"
command -v docker compose >/dev/null 2>&1 || \
command -v docker-compose >/dev/null 2>&1 || error "Docker Compose가 필요합니다."

# ── 2. .env 파일 확인 ──────────────────────────────────────
if [ ! -f ".env" ]; then
    warn ".env 파일이 없습니다. .env.example에서 복사합니다..."
    cp .env.example .env
    warn "⚠️  .env 파일을 열어 ANTHROPIC_API_KEY를 설정하세요!"
    echo ""
    echo "  nano .env"
    echo ""
    read -p "지금 설정하겠습니까? (y/N): " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        ${EDITOR:-nano} .env
    else
        error "ANTHROPIC_API_KEY 없이는 Claude Vision 분석이 불가합니다."
    fi
fi

# API 키 확인
source .env 2>/dev/null || true
if [ -z "$ANTHROPIC_API_KEY" ] || [ "$ANTHROPIC_API_KEY" = "your_anthropic_api_key_here" ]; then
    error ".env에서 ANTHROPIC_API_KEY를 설정하세요."
fi
info "API 키 확인 완료 ✓"

# ── 3. 기존 컨테이너 정리 ──────────────────────────────────
info "기존 컨테이너 정지 중..."
docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true

# ── 4. 이미지 빌드 ─────────────────────────────────────────
info "Docker 이미지 빌드 중... (최초 빌드 시 5~10분 소요)"
docker compose build --no-cache

# ── 5. 서비스 시작 ─────────────────────────────────────────
info "서비스 시작 중..."
docker compose up -d

# ── 6. 헬스체크 ────────────────────────────────────────────
info "서버 응답 대기 중..."
for i in $(seq 1 30); do
    if curl -sf http://localhost/  >/dev/null 2>&1 || \
       curl -sf http://localhost:3000/ >/dev/null 2>&1; then
        echo ""
        info "✅ 배포 완료!"
        echo ""
        SERVER_IP=$(curl -sf https://api.ipify.org 2>/dev/null || echo "YOUR_SERVER_IP")
        echo -e "  🌐 접속 주소: ${GREEN}http://${SERVER_IP}${NC}"
        echo -e "  📊 로그 확인: docker compose logs -f app"
        echo -e "  🔄 재시작:    docker compose restart app"
        echo -e "  🛑 중지:      docker compose down"
        echo ""
        exit 0
    fi
    echo -n "."
    sleep 2
done

warn "서버가 응답하지 않습니다. 로그를 확인하세요:"
docker compose logs --tail=30 app
