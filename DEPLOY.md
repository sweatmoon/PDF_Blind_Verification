# 🚀 클라우드 서버 배포 가이드

## 권장 서버 사양

| 구분 | 최소 | 권장 |
|---|---|---|
| **CPU** | 2코어 | 4코어 |
| **RAM** | 4GB | 8GB |
| **디스크** | 30GB | 50GB |
| **OS** | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |

> 100페이지 PDF 기준 처리 시간: 권장 사양에서 약 **30~50초**

## 추천 클라우드 서비스 (월 비용)

| 서비스 | 사양 | 가격 | 특징 |
|---|---|---|---|
| **AWS EC2 t3.medium** | 2코어/4GB | ~$35/월 | 안정적, 한국 리전 있음 |
| **AWS EC2 t3.large** | 2코어/8GB | ~$65/월 | 권장 |
| **GCP e2-medium** | 2코어/4GB | ~$25/월 | 저렴 |
| **Hetzner CX32** | 4코어/8GB | ~$15/월 | 매우 저렴, 유럽 |
| **DigitalOcean 8GB** | 2코어/8GB | ~$48/월 | 간단한 UI |
| **Vultr 4GB** | 2코어/4GB | ~$24/월 | 한국 리전 있음 |

---

## 배포 절차

### 1단계: 서버 준비 (Ubuntu 22.04)

```bash
# 서버 SSH 접속
ssh ubuntu@YOUR_SERVER_IP

# 시스템 업데이트
sudo apt-get update && sudo apt-get upgrade -y

# Docker 설치
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Docker Compose 확인
docker compose version
```

### 2단계: 코드 다운로드

```bash
# Git 설치 및 코드 클론
sudo apt-get install -y git
git clone https://github.com/YOUR_GITHUB_USERNAME/blind-verify.git
cd blind-verify
```

### 3단계: 환경 변수 설정

```bash
# .env 파일 생성
cp .env.example .env
nano .env
```

`.env` 파일 내용:
```env
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxx   # 필수!
CLAUDE_MODEL=claude-sonnet-4-20250514
MAX_FILE_SIZE_MB=300
AUTO_DELETE_MIN=30
```

### 4단계: 배포 실행

```bash
bash deploy.sh
```

완료되면 `http://YOUR_SERVER_IP` 로 접속 가능합니다.

---

## 업데이트 방법

코드 수정 후 서버에서:
```bash
cd blind-verify
bash update.sh
```

---

## HTTPS 설정 (도메인 있을 때)

### Let's Encrypt 무료 SSL

```bash
# Certbot 설치
sudo apt-get install -y certbot

# 인증서 발급 (80 포트가 열려있어야 함)
sudo certbot certonly --standalone -d your-domain.com

# 인증서를 nginx/certs 폴더로 복사
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem nginx/certs/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem  nginx/certs/
sudo chmod 644 nginx/certs/*.pem

# nginx.conf에서 HTTPS 블록 주석 해제 후 재시작
docker compose restart nginx
```

### 자동 인증서 갱신
```bash
# crontab에 추가
(crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet && docker compose -f /home/ubuntu/blind-verify/docker-compose.yml restart nginx") | crontab -
```

---

## 방화벽 설정

```bash
sudo ufw allow 22    # SSH
sudo ufw allow 80    # HTTP
sudo ufw allow 443   # HTTPS
sudo ufw enable
```

---

## 유용한 명령어

```bash
# 로그 실시간 확인
docker compose logs -f app

# 컨테이너 상태 확인
docker compose ps

# 재시작
docker compose restart app

# 전체 중지
docker compose down

# 디스크 정리 (오래된 임시파일)
docker compose exec app find /app/tmp -mtime +1 -delete
```

---

## 처리 시간 예상 (권장 사양 기준)

| 파일 크기 | 페이지 수 | 업로드 | Vision 분석 | 전체 |
|---|---|---|---|---|
| 10MB | 20p | ~1초 | ~10초 | **~15초** |
| 50MB | 80p | ~3초 | ~35초 | **~45초** |
| 100MB | 150p | ~5초 | ~60초 | **~70초** |

> 병렬 5배치 처리 기준. Claude API 응답 속도에 따라 변동.
