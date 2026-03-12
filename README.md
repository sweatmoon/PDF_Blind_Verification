# 입찰 제안서 블라인드 검증 서비스

공공입찰 제안서(PDF)에서 블라인드 위반 요소를 자동으로 검출하는 실무형 검수 도구입니다.  
**Claude AI**가 최종 심사관 역할을 수행하며, 규칙 기반 탐지 + OCR + Claude 의미 판정의 3단계 파이프라인으로 동작합니다.

---

## 🚀 서비스 URL

| 환경 | URL |
|------|-----|
| 로컬 | `http://localhost:3000` |
| 샌드박스 | `https://3000-iatzp3t3oyoppl8bwtetb-c81df28e.sandbox.novita.ai` |

---

## ✅ 구현된 기능

### 핵심 기능
- **PDF 업로드 및 검증** – 드래그&드롭 지원, 최대 100MB
- **3단계 검증 파이프라인**
  1. 규칙 기반 탐지 (정규식 + 관리자 사전)
  2. OCR 이미지 텍스트 분석 (Tesseract kor+eng)
  3. Claude AI 의미 판정 (최종 심사관)
- **페이지별 결과 리포트** – 위반/주의/허용 분류, 판정 사유, 수정 권고안
- **블라인드 위험도 표시** – LOW / MEDIUM / HIGH
- **리포트 다운로드** – JSON / HTML 형식
- **파일 즉시 삭제** – 검증 완료 후 원본 및 중간 파일 즉시 삭제
- **TTL 자동 삭제** – 30분 후 자동 만료 삭제 스케줄러

### 관리자 기능
- **금칙어 사전 관리** – 업체명, 대표자명, 참여인력명, 이메일, 도메인, 브랜드명
- **간접 식별어 사전** – 고유 색상명, 내부 솔루션명, 슬로건
- **허용어 사전** – 발주기관명, 약칭, 대상사업명, 공공기관 공식명칭
- **시스템 통계 & 로그** 조회

### 보안
- HTTPS 통신 (배포 시)
- PDF 헤더 검증 (악성 파일 차단)
- 파일명 sanitize 처리
- 보안 삭제 (파일 내용 0으로 덮어쓰기 후 삭제)

---

## 🔍 검출 대상

| 구분 | 항목 |
|------|------|
| **위반** | 업체명, 대표자명, 참여인력 실명, 로고/CI/BI, 이메일, URL/도메인, 회사명 캡처, 메타데이터, 사업자번호 |
| **주의** | 회사 고유 색상명, 내부 솔루션명, 특정 업체 고유 슬로건, 간접 식별 표현 |
| **허용** | 발주기관명·로고, 대상사업명, 일반 기술 설명 |

---

## ⚙️ 설치 및 실행

### 1. 시스템 요구사항
```bash
sudo apt-get install -y tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng poppler-utils
pip3 install fastapi uvicorn python-multipart pymupdf pytesseract Pillow aiofiles anthropic pdfplumber
```

### 2. Claude API 키 설정 (선택)
```bash
cp .env.example .env
# .env 파일에서 ANTHROPIC_API_KEY 설정
# 미설정 시 규칙 기반 탐지만 동작 (Claude 판정 비활성)
export ANTHROPIC_API_KEY=sk-ant-...
```

### 3. 서비스 실행
```bash
cd /home/user/webapp
ANTHROPIC_API_KEY=sk-ant-... pm2 start ecosystem.config.cjs
# 또는 직접 실행:
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 3000
```

---

## 🏗️ 기술 스택

| 레이어 | 기술 |
|--------|------|
| Frontend | HTML/CSS/JS SPA (Tailwind CSS CDN) |
| Backend | Python FastAPI + uvicorn |
| PDF 파싱 | PyMuPDF (fitz) |
| OCR | Tesseract kor+eng |
| AI 판정 | Anthropic Claude (claude-3-5-sonnet-20241022) |
| 파일 관리 | 즉시 삭제 + TTL 스케줄러 |
| 프로세스 | PM2 |

---

## 📁 프로젝트 구조

```
webapp/
├── backend/
│   ├── main.py                    # FastAPI 진입점
│   ├── api/
│   │   ├── verify.py              # 업로드/상태/리포트 API
│   │   └── admin.py               # 관리자 사전/설정 API
│   ├── services/
│   │   ├── pipeline.py            # 검증 파이프라인 조율
│   │   ├── pdf_service.py         # PDF 파싱 (PyMuPDF)
│   │   ├── ocr_service.py         # OCR (Tesseract)
│   │   ├── rule_detector.py       # 규칙 기반 탐지
│   │   ├── claude_judge.py        # Claude AI 판정
│   │   └── file_manager.py        # 보안 파일 삭제/TTL
│   ├── models/
│   │   └── schemas.py             # Pydantic 데이터 모델
│   └── core/
│       └── config.py              # 설정/유틸리티
├── frontend/
│   └── public/
│       └── index.html             # SPA 프론트엔드
├── data/                          # 사전 파일 (dictionary.json)
├── tmp/                           # 임시 파일 (자동 삭제)
├── logs/                          # 로그
├── ecosystem.config.cjs           # PM2 설정
└── .env.example                   # 환경변수 예시
```

---

## 🔒 보안 정책

- 원본 PDF는 임시 저장소에만 저장, 검증 완료 즉시 삭제
- OCR 이미지, 캐시, 임시 JSON 모두 즉시 삭제
- 30분 후 TTL 기반 자동 삭제 (5분 주기 스케줄러)
- 결과 리포트에 원본 전문 저장 금지 (검출 항목만 저장)
- 관리자 로그에 원문 전체 텍스트 저장 금지

---

## 📈 2차 버전 예정

- [ ] PPTX / DOCX 지원
- [ ] 로고 이미지 유사도 매칭
- [ ] 팀별 사전 템플릿
- [ ] 검수 이력 비교
- [ ] REST API 외부 제공
