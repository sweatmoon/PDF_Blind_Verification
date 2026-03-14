"""
Claude Vision 판정 엔진 v6
- Computer Vision 우선 + Claude Vision 보조 아키텍처
- MediaPipe BlazeFace + OpenCV Haar: 얼굴 탐지 1선 (결정론적)
- pHash: 로고 유사도 1차 필터 (결정론적)
- SSIM + ORB: 로고 유사도 2차 확인
- PAGE 블록 구조로 페이지 간 전이 완전 차단
- 인물사진 판정 순서:
    1단계 MediaPipe BlazeFace (얼굴 landmark) → 실제 얼굴 확정
    2단계 OpenCV Haar Cascade → 보조 탐지
    3단계 색상/채도/피부색 휴리스틱 → 아이콘/실루엣 필터
    4단계 Claude Vision → 불명확 케이스만 호출
- 로고 판정 순서:
    1단계 pHash distance ≤ 8 → 즉시 위반 확정 (Claude 불필요)
    2단계 SSIM + ORB threshold 0.70↑ → 위반 확정
    3단계 threshold 미달 → 심볼 비교 단계
"""
from __future__ import annotations
import json, re, base64, io
from typing import List, Optional
import core.config as _cfg
from core.config import get_logger

logger = get_logger("claude_judge")

# ── 공공기관/발주기관 허용 키워드 (로고 절대 위반 불가) ──────────────
_PUBLIC_ORG_KEYWORDS = (
    "국가철도공단", "KR", "행정안전부", "LH", "한국토지주택공사",
    "한국전력", "한국전력공사", "KEPCO", "한국도로공사", "한국수자원공사",
    "한국가스공사", "한국철도공사", "코레일", "한국공항공사", "인천국제공항공사",
    "국민건강보험", "국민건강보험공단", "국민연금", "국민연금공단",
    "건강보험심사평가원", "국토교통부", "과학기술정보통신부",
    "교육부", "고용노동부", "보건복지부", "환경부", "문화체육관광부",
    "농림축산식품부", "산업통상자원부", "중소벤처기업부", "국방부",
    "경찰청", "소방청", "기상청", "통계청", "조달청", "특허청",
    "식품의약품안전처", "금융위원회", "공정거래위원회",
    "한국농어촌공사", "NIA", "NIPA", "KISA", "ETRI", "KAIST", "KIST",
    "정부24", "민원24", "나라장터", "디지털서비스",
)

# ── 시스템 프롬프트 ─────────────────────────────────────────────
SYSTEM_PROMPT = """너는 공공입찰 제안서 블라인드 검증 전문 심사관이다.
목표는 제안사(입찰자) 또는 참여인력을 식별할 수 있는 정보를 찾아내는 것이다.

━━━ 핵심 판정 원칙 ━━━
입력은 [PAGE N START] ... [PAGE N END] 블록으로 구분된다.
각 블록은 완전히 독립적으로 판단하라.
★★★ 다른 PAGE 블록에서 발견한 내용을 현재 블록에 적용하는 것 절대 금지 ★★★
★★★ 어떤 페이지에 위반이 있어도 다른 페이지에 자동 확장 금지 ★★★
★★★ 특히 로고: 한 페이지에 로고가 있다고 해서 다른 페이지에도 로고가 있다고 추정 금지 ★★★

━━━ 우선순위 1 — 사전 등록 실명 (절대 위반) ━━━
- "제안사 식별 사전"의 실명이 이미지에 전체 이름으로 확인되면 → 【위반】
- 글자 사이 공백·점·기호를 제거했을 때 전체 이름과 일치하면 위반
  예: "홍 길 동", "홍·길동", "홍_길동" → "홍길동"과 일치 → 위반
- ★ 이름의 일부 글자만 우연히 포함된 경우는 위반 아님 ★
  예: 실명 목록에 "국민"이 있어도 "국민건강보험공단"은 위반 아님
  예: 실명 목록에 "전일"이 있어도 "전일제 근무"는 위반 아님

━━━ 우선순위 2 — 이름+신원정보 조합 (위반) ━━━
사전에 없는 이름이라도 아래 조합이 보이면 → 【위반】
단, 반드시 "사람 정보 문맥" 안에서만 적용하라:

【위반으로 판단하는 문맥】
- 인력 소개표, 조직도, 프로필 카드, 명함 형태
- "참여인력:", "담당자:", "PM:", "PL:", "책임자:" 등 인력 라벨 뒤의 이름
- 2~4글자 한글 이름 + 직책이 같은 셀/행/카드에 명확히 묶여 있는 경우
- 이름 + "/" 또는 "·" + 직책 형태 (예: "홍길동 / 수석감리원")

【위반이 아닌 문맥 — 절대 위반 처리 금지】
- 일반 본문 텍스트 (설명문, 제안 내용)
- 표 제목, 항목 설명
- 기관명·단체명 (예: "품질 관리원", "개발 책임자"가 부서명인 경우)
- 기술 용어, 업무 용어 (예: "전일 작업", "국민 편의")
- 뉴스 기사, 스크린샷, UI 캡처 이미지 속 텍스트

━━━ 우선순위 3 — 업체 식별 정보 (위반) ━━━
- 반드시 "제안사 식별 사전"에 등록된 항목이 이미지에서 보일 때만 위반
- 사전에 없는 회사명·영문명·도메인·이메일은 절대 위반 판정 금지
- 사전에 없는 텍스트를 임의로 회사 관련 정보로 추정하는 것 엄격히 금지
- 뉴스 기사·스크린샷·UI 이미지 속 텍스트를 제안사 정보로 오인 금지

━━━ 우선순위 4 — 인물 사진 ━━━

★★★ 핵심 원칙: "사람 형태"가 보인다고 절대 위반 판정하지 마라 ★★★
★★★ 반드시 "카메라로 촬영된 실제 사람 사진"인지만 기준으로 삼아라 ★★★

【1단계: 인물 사진 후보 탐지 (이 단계는 의심 항목을 태그하는 것, 위반 확정 아님)】
아래 중 하나라도 해당하면 type="인물사진" 또는 type="얼굴사진"으로 표시하고 bbox를 포함하라:
  1. 피부 질감이 사진처럼 보인다 (픽셀 질감, 사진 해상도)
  2. 눈·코·입 이목구비가 실제 사람처럼 식별 가능하다
  3. 카메라로 촬영된 실사 이미지처럼 보인다

★ 인물 사진으로 의심되면 반드시 bbox와 함께 "위반" 또는 "주의"로 제출하라
★ 최종 위반 확정은 후처리 코드에서 bbox crop 이미지 재분류로 결정됨
★ 실사 사진 같으면 일단 「위반」으로, 애매하면 「주의」로 표시하라
   → 후처리에서 이미지 재판정으로 확인하므로 오탐 걱정 없음

해당하는 경우:
- 피부 질감이 보이는 실사 인물 사진
- 눈·코·입이 명확히 식별되는 사진
- 인물 프로필 사진 (증명사진, 여권사진 포함)
- 행사/인터뷰 촬영 사진, 명함 사진
- 여러 인물의 얼굴 사진 배열 (조직도 포함)

【2단계: 그래픽/아이콘이 명확한 경우 — 1단계에서 제외 (검출하지 않아도 됨)】
아래 유형은 그래픽임이 명확하면 결과에 포함하지 않아도 된다:
- 사람 아이콘 (단색, 선화, 색상 무관)
- 픽토그램 (화장실 표지판 스타일 포함)
- 실루엣 그래픽 (단색 또는 그라데이션 실루엣)
- 캐릭터 일러스트 (카툰, 애니메이션, 만화 스타일)
- 벡터 스타일 사람 그림 (선이 깔끔하고 평면적인 그림)
- 단색 인물 아이콘 (한 가지 색상으로만 표현된 사람 형태)
- 얼굴 디테일이 없는 사람 그래픽 (눈코입 없음)
- 시스템 다이어그램·플로우차트 속 사람 아이콘
- 연구자/직원/사용자 아이콘 (소프트웨어 UI 스타일)
- AI 생성 일러스트, 3D 렌더링 캐릭터
- 얼굴이 흐릿하거나 식별 불가한 이미지
- 먼 거리 촬영, 뒷모습, 작은 썸네일

판단 기준:
  Q. "이 이미지는 카메라로 촬영된 실제 사람 사진인가?"
  → YES 또는 가능성 있음 → 반드시 bbox와 함께 【위반】 또는 【주의】로 표시
  → 그래픽/아이콘이 명확 → 결과에서 제외하거나 【허용】
  → 불확실 → 【주의】로 표시 (후처리에서 재판정)

  Q. "사람 형태가 보이는 그래픽/아이콘/일러스트인가?"
  → YES (명확히 그래픽) → 결과에서 제외
  → 불확실 → 【주의】로 표시

★★★ 인물 사진 탐지 시 반드시 bbox를 포함하라 — 이미지 재검증에 사용됨 ★★★
- 인물 후보(위반/주의) 탐지 시 반드시 해당 인물이 위치한 bbox를 [x1,y1,x2,y2]로 표시
- bbox를 특정할 수 없어도 최대한 추정값을 포함하라 (null 사용 지양)
- bbox가 없으면 후처리 재검증을 수행할 수 없어 오탐 방지가 불가능해짐

━━━ 우선순위 4-B — 회사 로고 판정 (매우 보수적으로) ━━━

【로고 위반 판정 조건 — 아래 4가지 모두 충족해야만 위반】
1. 레퍼런스 로고의 심볼 형태가 명확히 동일
2. 워드마크 텍스트가 동일
3. 색상 패턴이 동일
4. 레이아웃 구조가 동일
→ 위 조건 중 하나라도 불명확하면 반드시 【허용】으로 판정

【로고 절대 위반 불가 대상】
다음 기관의 로고는 제안사 로고로 절대 판정하지 마라:
- 국가철도공단(KR), 행정안전부, LH, 한국토지주택공사, 한국전력(KEPCO)
- 한국도로공사, 한국수자원공사, 한국철도공사(코레일), 국민건강보험공단
- 기타 모든 공공기관·정부기관·발주기관 로고 → 항상 【허용】

【로고가 아닌 것 — 절대 로고로 판정 금지】
다음 요소는 회사 로고가 아니다:
- 강조 표시용 빨간 원 (발표 강조, 마킹 표시)
- 다이어그램 배지 (번호 표시, 단계 아이콘)
- UI 강조 도형 (버튼, 뱃지, 태그)
- 인포그래픽 아이콘 (화살표, 체크마크, 도형)
- 단순 원형·사각형·다각형 그래픽
- 슬라이드 장식 요소

【우측하단 영역】
우측하단은 참고적으로만 확인하라.
실제 회사명 또는 레퍼런스 로고가 명확히 보이지 않으면 로고 위반으로 절대 판정하지 마라.
단순 도형, 배경 요소, 저작권 표시 등은 위반 아님.

【로고 레퍼런스가 있는 경우】
- 이 분석은 1차 후보 탐지이다. 로고가 의심되면 type="로고후보"로 표시하라.
- 최종 위반 판정은 후보 영역 crop 후 재비교로 결정된다.
- 레퍼런스와 형태·워드마크·색상·레이아웃이 모두 명확히 일치해야만 type="로고" 위반으로 표시 가능
- 발주기관·공공기관 로고는 절대 위반 아님

【로고 레퍼런스가 없는 경우】
- 사전에 등록된 회사명이 로고 형태로 명확히 보이면 → 【주의】 (위반 아님)
- 레퍼런스 없이 로고라고 단정 짓는 것 금지

━━━ 우선순위 5 — 간접 식별 정보 (주의) ━━━
- 사전 등록 솔루션명·슬로건·색상명·조직명이 보이면 → 【주의】

━━━ 익명 처리 인정 ━━━
- OOO, ○○○, ***, 홍○○, 홍길○, □□□, ?? ?? 등 마스킹 기호 → 【허용】
- 마스킹된 이름 + 직책 조합도 이름이 마스킹됐으면 → 【허용】

━━━ 텍스트 후보 탐지 결과 처리 ━━━
이미지 안에 [PAGE N 텍스트 후보] 블록이 있으면:
- 이 항목들은 텍스트 레이어에서 추출된 "후보"이다
- 반드시 이미지에서 해당 내용을 직접 확인한 후 최종 판정하라
- 이미지에서 확인되면 → 해당 판정(위반/주의)으로 출력
- 이미지에서 확인되지 않으면 → 출력하지 마라
- 후보 항목은 반드시 명시된 페이지 번호로만 귀속시킬 것

━━━ 마스킹 처리된 이름 ━━━
- "O O O", "OOO", "***", "□□□", "?? ??" 등 → 무조건 【허용】

━━━ 절대 금지 ━━━
- 다른 PAGE 블록의 내용을 현재 PAGE 판정에 참조하는 것
- 사전 실명 일부 글자만으로 위반 판정하는 것
- 사전에 없는 항목을 임의로 위반 처리하는 것
- 뉴스·스크린샷·UI 속 텍스트를 제안사 정보로 판정하는 것
- 레퍼런스 없이 로고를 위반으로 판정하는 것
- 일반 본문/기술 설명에서 이름+직책 규칙 적용하는 것
- 공공기관 로고를 제안사 로고로 오인하는 것
- 단색 실루엣·픽토그램을 인물 사진으로 오인하는 것
- 아이콘·일러스트·벡터 그래픽을 인물 사진으로 오인하는 것
- 사람 형태만 보고 인물 사진 위반으로 판정하는 것 (형태 ≠ 실사 사진)
- 얼굴 디테일이 없는 사람 그래픽을 인물 사진으로 판정하는 것
- 단색 인물 아이콘, 연구자/직원/사용자 아이콘을 실사 사진으로 오인하는 것
- 강조 그래픽(빨간 원, 배지, 도형)을 로고로 오인하는 것
- 한 페이지의 로고 존재를 다른 페이지에 전이하는 것
- 4가지 조건(형태+워드마크+색상+레이아웃)이 모두 충족되지 않았는데 로고 위반으로 판정하는 것

━━━ 출력 형식 ━━━
반드시 아래 JSON 형식으로만 반환하라. 다른 텍스트 절대 포함 금지:
{
  "items": [
    {
      "page": "페이지 번호 (정수, 예: 5)",
      "type": "검출 유형",
      "content": "검출 내용",
      "judgment": "위반 또는 주의 또는 허용",
      "reason": "판정 사유",
      "recommendation": "수정 권고 (허용이면 빈 문자열)",
      "bbox": [x1, y1, x2, y2]
    }
  ]
}
- bbox는 이미지 내 검출 영역의 픽셀 좌표 (좌상단 x1,y1 / 우하단 x2,y2)
- bbox를 특정할 수 없어도 최대한 추정값을 포함하라 (null 사용 지양)
- 로고/로고후보 탐지 시 반드시 bbox를 포함하라 — 재비교에 사용됨
- 인물사진/인물/얼굴 탐지 시 반드시 bbox를 포함하라 — 이미지 재검증에 사용됨
- bbox가 없으면 후처리 재검증을 수행할 수 없어 오탐 방지가 불가능해짐
- 문제 없는 페이지는 포함하지 않아도 된다
- 한 페이지에 위반 요소 N개면 items 배열에 N개 항목
- 로고 후보(1차 탐지)는 type="로고후보"로 표시하고 judgment="주의"로 설정
- 최종 위반 확정은 코드에서 bbox crop 후 SSIM/ORB 재비교로 결정
- 인물 최종 위반 확정은 코드에서 bbox crop 후 이미지 재분류로 결정"""


# ── threshold ────────────────────────────────────────────────────
_LOGO_SIM_THRESHOLD        = 0.70  # 전체 로고 SSIM/ORB 임계값 (0.70↑ → 위반)
_SYMBOL_SIM_THRESHOLD      = 0.65  # 심볼 비교 임계값 (전체보다 완화)
_SYMBOL_TEMPLATE_THRESHOLD = 0.55  # template matching 임계값

# 워드마크 후보 키워드 (company_dict 없을 때 기본 사용)
# 실제 사용 시 company_dict의 company_names/english_names/abbreviations에서 동적 생성
_DEFAULT_WORDMARK_CANDIDATES: tuple = (
    "activo", "악티보", "주식회사 악티보",
)


# ── 공통 crop 추출 유틸 ──────────────────────────────────────────
def _extract_crop(
    page_b64: str,
    bbox: Optional[list],
    pad: int = 10,
) -> Optional[object]:
    """
    페이지 이미지(base64)에서 bbox 기반 crop PIL Image 반환.
    bbox None → 우측하단 fallback.
    """
    try:
        from PIL import Image
        page_bytes = base64.b64decode(page_b64)
        page_img   = Image.open(io.BytesIO(page_bytes)).convert("RGB")
        pw, ph = page_img.width, page_img.height

        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(pw, x2 + pad), min(ph, y2 + pad)
            logger.debug(f"bbox crop: ({x1},{y1},{x2},{y2}) / 페이지 {pw}×{ph}")
        else:
            logger.debug("bbox 없음 → fallback crop (우측하단 28%×22%)")
            x1, y1 = int(pw * 0.72), int(ph * 0.78)
            x2, y2 = pw, ph

        return page_img.crop((x1, y1, x2, y2))
    except Exception as e:
        logger.warning(f"crop 추출 실패: {e}")
        return None


# ── 로고 후보 재비교: pHash → SSIM → ORB → red_mask 순차 비교 ──
def _verify_logo_candidate(
    page_b64: str,
    logo_ref_b64: str,
    bbox: Optional[list] = None,
    page_media_type: str = "image/jpeg",
) -> bool:
    """
    Claude Vision이 반환한 bbox 영역을 crop 후 레퍼런스 로고와 비교.

    비교 순서 (결정론적 → 점진적 정밀도):
      0단계: pHash (distance ≤ 8) → 즉시 위반 확정 (Claude 불필요, 빠름)
      1단계: SSIM ≥ 0.70          → 위반 확정
      2단계: ORB ≥ 0.70           → 위반 확정
      3단계: red_mask SSIM ≥ 0.70 → 위반 확정

    반환값: True = 전체 로고 일치 (위반 확정), False = 불일치
    """
    try:
        import numpy as np
        from PIL import Image

        ref_bytes = base64.b64decode(logo_ref_b64)
        ref_img   = Image.open(io.BytesIO(ref_bytes)).convert("RGB")
        ref_w, ref_h = ref_img.size

        crop_img = _extract_crop(page_b64, bbox)
        if crop_img is None:
            return False

        # bbox=None fallback 처리
        crop_area = crop_img.width * crop_img.height
        ref_area  = ref_w * ref_h
        if bbox is None and crop_area < ref_area * 0.3:
            page_bytes = base64.b64decode(page_b64)
            crop_img = Image.open(io.BytesIO(page_bytes)).convert("RGB")
            logger.debug(f"bbox=None fallback → 페이지 전체 이미지: {crop_img.size}")

        # ── 0단계: pHash (결정론적 1차 필터) ────────────────────────
        phash_dist = _compare_phash(ref_img, crop_img)
        if 0.0 <= phash_dist <= _PHASH_MATCH_THRESHOLD:
            logger.info(
                f"[전체로고] pHash distance={phash_dist:.0f} ≤ {_PHASH_MATCH_THRESHOLD}"
                f" → 위반 확정 (Claude 불필요)"
            )
            return True
        logger.debug(f"[전체로고] pHash distance={phash_dist:.0f} > {_PHASH_MATCH_THRESHOLD}")

        # ── 비교 크기: 레퍼런스 비율 유지 ───────────────────────────
        aspect = ref_w / max(ref_h, 1)
        cmp_h  = 80
        cmp_w  = max(80, int(cmp_h * aspect))
        cmp_size = (cmp_w, cmp_h)

        crop_np = np.array(crop_img.resize(cmp_size, Image.LANCZOS))
        ref_np  = np.array(ref_img.resize(cmp_size,  Image.LANCZOS))

        # ── 1단계: SSIM ─────────────────────────────────────────────
        ssim_score = _compute_ssim(ref_np, crop_np)
        logger.debug(f"[전체로고] SSIM={ssim_score:.3f}")
        if ssim_score >= _LOGO_SIM_THRESHOLD:
            logger.info(f"전체 로고 SSIM={ssim_score:.3f} → 위반 확정")
            return True

        # ── 2단계: ORB ──────────────────────────────────────────────
        orb_score = _compute_orb(ref_np, crop_np)
        logger.debug(f"[전체로고] ORB={orb_score:.3f}")
        if orb_score >= _LOGO_SIM_THRESHOLD:
            logger.info(f"전체 로고 ORB={orb_score:.3f} → 위반 확정")
            return True

        # ── 3단계: 빨간 마스크 SSIM ─────────────────────────────────
        ref_full_np  = np.array(ref_img)
        crop_full_np = np.array(crop_img)
        red_score = _compute_red_mask_ssim(ref_full_np, crop_full_np)
        logger.debug(f"[전체로고] red_mask={red_score:.3f}")
        if red_score >= _LOGO_SIM_THRESHOLD:
            logger.info(f"전체 로고 red_mask={red_score:.3f} → 위반 확정")
            return True

        logger.info(
            f"전체 로고 SSIM={ssim_score:.3f} ORB={orb_score:.3f} "
            f"red_mask={red_score:.3f} → 불일치(심볼 단계 진행)"
        )
        return False

    except ImportError as e:
        logger.warning(f"로고 재비교 라이브러리 미설치: {e} → 허용 처리")
        return False
    except Exception as e:
        logger.warning(f"로고 재비교 실패: {e} → 허용 처리")
        return False


# ── 전체 로고 이미지에서 심볼 영역 자동 추출 ─────────────────────
def _extract_symbol_from_logo(logo_b64: str) -> Optional[str]:
    """
    전체 로고 이미지에서 심볼 영역을 자동 추출해 base64로 반환.

    추출 전략 (우선순위 순):

    방법 A — 색상 기반 추출 (권장)
      빨간색/진한 색상 픽셀 클러스터의 bounding box를 계산해
      심볼 영역으로 사용. 해상도·여백 변화에 강건하다.
      조건:
        - 빨간색 마스크: H∈[0°,15°]∪[345°,360°], S≥40%, V≥30%
        - 마스킹 픽셀 비율 ≥ 2% (너무 작으면 의미 없음)
        - bounding box 가로 비율 ≤ 50% (심볼이 전체 로고의 절반 이하)

    방법 B — 좌측 비율 분리 (폴백)
      이미지 좌측 33%를 심볼 영역으로 사용.
      색상 기반 추출 실패 시 대안.

    반환값:
      base64 문자열 (PNG) → 심볼 추출 성공
      None                → 추출 실패 (라이브러리 없음, 이미지 오류 등)
    """
    if not logo_b64:
        return None
    try:
        import numpy as np
        from PIL import Image
        import cv2

        logo_bytes = base64.b64decode(logo_b64)
        logo_img   = Image.open(io.BytesIO(logo_bytes)).convert("RGB")
        logo_np    = np.array(logo_img)
        h, w       = logo_np.shape[:2]

        # ── 방법 A: 빨간색 픽셀 bounding box ─────────────────────
        # BGR→HSV 변환 후 빨간색 범위 마스킹
        bgr  = cv2.cvtColor(logo_np, cv2.COLOR_RGB2BGR)
        hsv  = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

        # 빨간색 범위 1: H=[0,15] (0°~30°)
        mask1 = cv2.inRange(hsv,
                            np.array([0,  100, 80]),
                            np.array([15, 255, 255]))
        # 빨간색 범위 2: H=[165,180] (330°~360°)
        mask2 = cv2.inRange(hsv,
                            np.array([165, 100, 80]),
                            np.array([180, 255, 255]))
        red_mask = cv2.bitwise_or(mask1, mask2)

        red_pixel_count  = int(np.sum(red_mask > 0))
        total_pixels     = h * w
        red_ratio        = red_pixel_count / max(total_pixels, 1)

        if red_ratio >= 0.02:  # 빨간 픽셀이 2% 이상
            # 빨간 픽셀들의 좌표 추출
            ys, xs = np.where(red_mask > 0)
            y1_r, y2_r = int(ys.min()), int(ys.max())
            x1_r, x2_r = int(xs.min()), int(xs.max())

            # 심볼이 로고 너비의 50% 이하인 경우만 유효
            sym_w = x2_r - x1_r
            if sym_w <= w * 0.5:
                # 약간 패딩 추가 (심볼 경계를 넉넉하게)
                pad = max(4, int(min(h, sym_w) * 0.1))
                x1_r = max(0, x1_r - pad)
                y1_r = max(0, y1_r - pad)
                x2_r = min(w, x2_r + pad)
                y2_r = min(h, y2_r + pad)

                symbol_img = logo_img.crop((x1_r, y1_r, x2_r, y2_r))
                buf = io.BytesIO()
                symbol_img.save(buf, format="PNG")
                sym_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                logger.info(
                    f"심볼 자동 추출 (방법 A — 색상 기반): "
                    f"bbox=({x1_r},{y1_r},{x2_r},{y2_r}), "
                    f"빨간픽셀비율={red_ratio:.1%}, "
                    f"심볼크기={x2_r-x1_r}×{y2_r-y1_r}"
                )
                return sym_b64
            else:
                logger.debug(
                    f"방법 A: 빨간 영역이 너무 넓음 (sym_w={sym_w}, w={w}) → 방법 B 시도"
                )
        else:
            logger.debug(
                f"방법 A: 빨간 픽셀 부족 ({red_ratio:.1%} < 2%) → 방법 B 시도"
            )

        # ── 방법 B: 좌측 33% 분리 (폴백) ─────────────────────────
        split_x = int(w * 0.33)
        if split_x >= 8:  # 너무 작으면 의미 없음
            symbol_img = logo_img.crop((0, 0, split_x, h))
            buf = io.BytesIO()
            symbol_img.save(buf, format="PNG")
            sym_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            logger.info(
                f"심볼 자동 추출 (방법 B — 좌측 33%): "
                f"size={split_x}×{h}"
            )
            return sym_b64

        logger.warning("심볼 자동 추출 실패: 이미지 크기 부족")
        return None

    except ImportError as e:
        logger.debug(f"심볼 추출 라이브러리 미설치: {e}")
        return None
    except Exception as e:
        logger.warning(f"심볼 자동 추출 오류: {e}")
        return None


# ── 심볼 비교: crop vs logo_symbol_reference ─────────────────────
def _verify_symbol_candidate(
    page_b64: str,
    symbol_ref_b64: str,
    bbox: Optional[list] = None,
) -> bool:
    """
    심볼 레퍼런스(logo_symbol_reference.png)와 crop 영역을 비교.
    전체 로고 비교보다 완화된 threshold 사용 (레이아웃 변화 대응).

    비교 순서:
      1. SSIM (grayscale)
      2. ORB feature match
      3. Template matching (cv2.matchTemplate)

    반환값:
      True  → 심볼 유사 (워드마크 추가 확인 필요)
      False → 심볼 불일치 (허용 처리)
    """
    try:
        import numpy as np
        import cv2
        from PIL import Image

        sym_bytes = base64.b64decode(symbol_ref_b64)
        sym_img   = Image.open(io.BytesIO(sym_bytes)).convert("RGB")
        sym_w, sym_h = sym_img.size

        crop_img = _extract_crop(page_b64, bbox, pad=15)
        if crop_img is None:
            return False

        # ── bbox=None fallback crop이 너무 작으면 페이지 전체 사용 ──
        crop_area = crop_img.width * crop_img.height
        sym_area  = sym_w * sym_h
        if bbox is None and crop_area < sym_area * 0.3:
            page_bytes = base64.b64decode(page_b64)
            crop_img = Image.open(io.BytesIO(page_bytes)).convert("RGB")
            logger.debug(f"bbox=None fallback → 페이지 전체 이미지 사용: {crop_img.size}")

        # ── 심볼 비율을 유지한 비교 크기 (96px 높이 기준) ──────────
        aspect   = sym_w / max(sym_h, 1)
        cmp_h    = 96
        cmp_w    = max(32, int(cmp_h * aspect))
        cmp_size = (cmp_w, cmp_h)

        crop_np = np.array(crop_img.resize(cmp_size, Image.LANCZOS))
        sym_np  = np.array(sym_img.resize(cmp_size,  Image.LANCZOS))

        # 1단계: SSIM
        ssim_score = _compute_ssim(sym_np, crop_np)
        logger.debug(f"[심볼] SSIM={ssim_score:.3f}")
        if ssim_score >= _SYMBOL_SIM_THRESHOLD:
            logger.info(f"심볼 SSIM 일치={ssim_score:.3f} → 심볼 검출 확정")
            return True

        # 2단계: ORB
        orb_score = _compute_orb(sym_np, crop_np)
        logger.debug(f"[심볼] ORB={orb_score:.3f}")
        if orb_score >= _SYMBOL_SIM_THRESHOLD:
            logger.info(f"심볼 ORB 일치={orb_score:.3f} → 심볼 검출 확정")
            return True

        # 3단계: Template matching (심볼 크기 비율 유지)
        try:
            crop_gray = cv2.cvtColor(crop_np, cv2.COLOR_RGB2GRAY)
            sym_gray  = cv2.cvtColor(sym_np,  cv2.COLOR_RGB2GRAY)
            # 심볼을 crop 절반 크기로 리사이즈해 sliding window 검색
            h, w  = crop_gray.shape
            th, tw = max(8, h // 2), max(8, w // 2)
            templ = cv2.resize(sym_gray, (tw, th))
            if templ.shape[0] <= crop_gray.shape[0] and templ.shape[1] <= crop_gray.shape[1]:
                result_map = cv2.matchTemplate(crop_gray, templ, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result_map)
                logger.debug(f"[심볼] Template={max_val:.3f}")
                if max_val >= _SYMBOL_TEMPLATE_THRESHOLD:
                    logger.info(f"심볼 Template 일치={max_val:.3f} → 심볼 검출 확정")
                    return True
        except Exception as _te:
            logger.debug(f"Template matching 실패: {_te}")

        # 4단계: 빨간 마스크 SSIM (배경색 독립 비교)
        # 누끼 심볼 레퍼런스와 흰 배경 대상 모두에서 빨간 픽셀을 이진화해 형태 비교
        sym_full_np  = np.array(sym_img)
        crop_full_np = np.array(crop_img)
        red_score = _compute_red_mask_ssim(sym_full_np, crop_full_np)
        logger.debug(f"[심볼] red_mask={red_score:.3f}")
        if red_score >= _SYMBOL_SIM_THRESHOLD:
            logger.info(f"심볼 red_mask 일치={red_score:.3f} → 심볼 검출 확정")
            return True

        logger.info(
            f"심볼 SSIM={ssim_score:.3f} ORB={orb_score:.3f} "
            f"red_mask={red_score:.3f} → 불일치"
        )
        return False

    except ImportError as e:
        logger.warning(f"심볼 비교 라이브러리 미설치: {e} → False")
        return False
    except Exception as e:
        logger.warning(f"심볼 비교 실패: {e} → False")
        return False


# ── 워드마크 근처 텍스트 존재 확인 ──────────────────────────────────
def _has_wordmark_nearby(
    page_b64: str,
    bbox: Optional[list],
    wordmark_candidates: Optional[list] = None,
    ocr_service=None,
) -> bool:
    """
    crop 영역(+ 아래쪽 확장 영역)에서 워드마크 텍스트가 존재하는지 확인.

    1차: OCR 서비스로 텍스트 추출 (Google Vision / Tesseract)
    2차: 기본 워드마크 후보 키워드 매칭

    wordmark_candidates: 사전에서 추출한 회사명/영문명/약칭 목록
    반환값: True → 워드마크 존재 (위반 확정), False → 텍스트 없음 (주의)
    """
    candidates = list(wordmark_candidates or _DEFAULT_WORDMARK_CANDIDATES)
    # 기본 후보 항상 포함
    for kw in _DEFAULT_WORDMARK_CANDIDATES:
        if kw not in candidates:
            candidates.append(kw)

    try:
        from PIL import Image
        import numpy as np

        page_bytes = base64.b64decode(page_b64)
        page_img   = Image.open(io.BytesIO(page_bytes)).convert("RGB")
        pw, ph = page_img.width, page_img.height

        # bbox 주변 + 아래 확장 영역 crop (워드마크는 심볼 아래/옆에 위치)
        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            # 가로 확장 10%, 아래쪽은 심볼 높이의 1.5배까지
            sym_h = max(20, y2 - y1)
            ex1 = max(0, x1 - 10)
            ey1 = max(0, y1 - 5)
            ex2 = min(pw, x2 + 10)
            ey2 = min(ph, y2 + int(sym_h * 1.5))
        else:
            # bbox 없으면 우측하단 넓은 영역
            ex1, ey1 = int(pw * 0.65), int(ph * 0.72)
            ex2, ey2 = pw, ph

        expanded_img = page_img.crop((ex1, ey1, ex2, ey2))

        ocr_text = ""
        # OCR 서비스가 있으면 우선 사용
        if ocr_service is not None:
            try:
                ocr_text = ocr_service.from_image(expanded_img) or ""
            except Exception:
                pass

        # Tesseract 폴백
        if not ocr_text.strip():
            try:
                import pytesseract
                ocr_text = pytesseract.image_to_string(
                    expanded_img, config="--psm 7 --oem 1"
                ) or ""
            except Exception:
                pass

        if ocr_text.strip():
            text_lower = ocr_text.lower()
            for cand in candidates:
                if cand.lower() in text_lower:
                    logger.info(f"워드마크 OCR 검출: '{cand}' in '{ocr_text[:60].strip()}'")
                    return True

        logger.debug(f"워드마크 미검출 (OCR='{ocr_text[:40].strip()}')")
        return False

    except Exception as e:
        logger.debug(f"워드마크 확인 실패: {e}")
        return False


# ── Computer Vision 유틸리티 ──────────────────────────────────────────────────

# MediaPipe 모델 경로 (백엔드 루트)
import os as _os
_MP_MODEL_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "blaze_face_short_range.tflite"
)

# 싱글톤 탐지기 (초기화 비용 절감)
_mp_face_detector = None
_cv_face_cascade  = None


def _get_mp_face_detector():
    """MediaPipe BlazeFace 탐지기 싱글톤 반환 (모델 파일 없으면 None)"""
    global _mp_face_detector
    if _mp_face_detector is not None:
        return _mp_face_detector
    try:
        if not _os.path.exists(_MP_MODEL_PATH):
            logger.warning(f"[face_detect] BlazeFace 모델 없음: {_MP_MODEL_PATH}")
            return None
        from mediapipe.tasks.python import vision as _mp_vision
        from mediapipe.tasks.python.core.base_options import BaseOptions as _BaseOpts
        opts = _mp_vision.FaceDetectorOptions(
            base_options=_BaseOpts(model_asset_path=_MP_MODEL_PATH),
            min_detection_confidence=0.5,
        )
        _mp_face_detector = _mp_vision.FaceDetector.create_from_options(opts)
        logger.info("[face_detect] MediaPipe BlazeFace 탐지기 초기화 완료")
        return _mp_face_detector
    except Exception as e:
        logger.warning(f"[face_detect] MediaPipe 초기화 실패: {e}")
        return None


def _get_cv_face_cascade():
    """OpenCV Haar Cascade 탐지기 싱글톤 반환"""
    global _cv_face_cascade
    if _cv_face_cascade is not None:
        return _cv_face_cascade
    try:
        import cv2
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _cv_face_cascade = cv2.CascadeClassifier(cascade_path)
        if _cv_face_cascade.empty():
            logger.warning("[face_detect] Haar cascade 로드 실패")
            return None
        logger.info("[face_detect] OpenCV Haar Cascade 초기화 완료")
        return _cv_face_cascade
    except Exception as e:
        logger.warning(f"[face_detect] OpenCV Haar 초기화 실패: {e}")
        return None


def _detect_real_face(pil_img) -> bool:
    """
    Computer Vision 기반 실제 얼굴 탐지 (결정론적).

    검사 순서:
      1단계: MediaPipe BlazeFace (신경망 기반, 높은 정밀도)
             → 얼굴 landmark 검출 시 즉시 True 반환
      2단계: OpenCV Haar Cascade (전통적, 빠른 폴백)
             → 얼굴 영역 검출 시 True 반환

    반환값:
      True  → 실제 사람 얼굴 존재 (위반 확정)
      False → 얼굴 없음 (아이콘/실루엣/일러스트 가능)

    아이콘·실루엣·벡터 일러스트·캐릭터는 얼굴 landmark가 없으므로
    False 반환 → 허용 처리.
    """
    try:
        import numpy as np
        import cv2

        if pil_img.width < 30 or pil_img.height < 30:
            logger.debug("[face_detect] 이미지 너무 작음 → False")
            return False

        img_rgb = np.array(pil_img.convert("RGB"))

        # ── 1단계: MediaPipe BlazeFace ──────────────────────────────
        mp_detector = _get_mp_face_detector()
        if mp_detector is not None:
            try:
                import mediapipe as _mp
                mp_img = _mp.Image(
                    image_format=_mp.ImageFormat.SRGB,
                    data=img_rgb.astype(np.uint8)
                )
                result = mp_detector.detect(mp_img)
                if result.detections:
                    conf = result.detections[0].categories[0].score \
                        if result.detections[0].categories else 0.0
                    logger.info(
                        f"[face_detect] MediaPipe 얼굴 검출 "
                        f"(count={len(result.detections)}, conf={conf:.2f}) → True"
                    )
                    return True
                logger.debug("[face_detect] MediaPipe: 얼굴 없음")
            except Exception as _me:
                logger.debug(f"[face_detect] MediaPipe 실패: {_me}")

        # ── 2단계: OpenCV Haar Cascade (폴백) ──────────────────────
        cascade = _get_cv_face_cascade()
        if cascade is not None:
            try:
                gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
                faces = cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=4,
                    minSize=(20, 20),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                if len(faces) > 0:
                    logger.info(
                        f"[face_detect] Haar Cascade 얼굴 검출 "
                        f"(count={len(faces)}) → True"
                    )
                    return True
                logger.debug("[face_detect] Haar: 얼굴 없음")
            except Exception as _ce:
                logger.debug(f"[face_detect] Haar 실패: {_ce}")

        return False

    except Exception as e:
        logger.warning(f"[face_detect] 전체 실패: {e} → False")
        return False


# ── pHash 기반 로고 유사도 1차 필터 (결정론적) ──────────────────────────────
_PHASH_MATCH_THRESHOLD = 8   # distance ≤ 8 → 동일 로고 (64-bit hash 기준)


def _compute_phash(pil_img):
    """
    PIL Image → perceptual hash.
    반환: imagehash.ImageHash 객체 (None on error)
    """
    try:
        import imagehash
        return imagehash.phash(pil_img)
    except Exception as e:
        logger.debug(f"[phash] 계산 실패: {e}")
        return None


def _compare_phash(pil_ref, pil_crop) -> float:
    """
    pHash distance 계산.
    반환: 0~64 (낮을수록 유사), -1.0 = 계산 실패
    """
    try:
        h1 = _compute_phash(pil_ref)
        h2 = _compute_phash(pil_crop)
        if h1 is None or h2 is None:
            return -1.0
        dist = float(h1 - h2)
        logger.debug(f"[phash] distance={dist:.0f}")
        return dist
    except Exception as e:
        logger.debug(f"[phash] 비교 실패: {e}")
        return -1.0


def _normalize_bg_to_white(np_img) -> object:
    """
    이미지 배경(검정)을 흰색으로 교체하는 정규화.

    레퍼런스 로고가 '투명 배경(누끼)을 검정으로 저장한 PNG'인 경우
    흰 배경 이미지와 SSIM 비교 시 0에 가깝게 나오는 문제를 해결.

    전략:
      1. 코너 평균 밝기 < 80 → 어두운 배경으로 판단
      2. 검정에 가까운 픽셀(R<40 & G<40 & B<40)만 흰색(255,255,255)으로 교체
         → 빨간 심볼·어두운 텍스트 등 전경 색상은 그대로 유지
      3. 밝은 배경이면 그대로 반환
    """
    try:
        import numpy as np
        h, w = np_img.shape[:2]
        cs = max(1, min(10, h // 6, w // 6))
        corners = np.concatenate([
            np_img[:cs, :cs].reshape(-1, 3),
            np_img[:cs, -cs:].reshape(-1, 3),
            np_img[-cs:, :cs].reshape(-1, 3),
            np_img[-cs:, -cs:].reshape(-1, 3),
        ], axis=0)
        if corners.mean() >= 80:
            # 이미 밝은 배경 → 그대로 반환
            return np_img
        # 어두운 배경: 검정에 가까운 픽셀만 흰색으로 교체
        out = np_img.copy()
        dark_mask = (out[:, :, 0] < 40) & (out[:, :, 1] < 40) & (out[:, :, 2] < 40)
        out[dark_mask] = [255, 255, 255]
        return out.astype(np_img.dtype)
    except Exception:
        return np_img


def _compute_ssim(ref_np, crop_np) -> float:
    """
    Grayscale SSIM 유사도 (0~1).
    비교 전 배경색을 흰색으로 자동 정규화하여
    '검정 배경 레퍼런스 vs 흰 배경 대상' 문제를 해결.
    """
    try:
        from skimage.metrics import structural_similarity as ssim
        import cv2
        ref_norm  = _normalize_bg_to_white(ref_np)
        crop_norm = _normalize_bg_to_white(crop_np)
        ref_gray  = cv2.cvtColor(ref_norm,  cv2.COLOR_RGB2GRAY)
        crop_gray = cv2.cvtColor(crop_norm, cv2.COLOR_RGB2GRAY)
        score, _ = ssim(ref_gray, crop_gray, full=True)
        return float(max(0.0, score))
    except Exception as e:
        logger.debug(f"SSIM 실패: {e}")
        return 0.0


def _compute_orb(ref_np, crop_np) -> float:
    """
    ORB feature match 기반 유사도 (0~1).
    매칭 비율 = good_matches / max(kp_ref, kp_crop).
    비교 전 배경색을 흰색으로 자동 정규화.
    """
    try:
        import cv2
        ref_norm  = _normalize_bg_to_white(ref_np)
        crop_norm = _normalize_bg_to_white(crop_np)
        ref_gray  = cv2.cvtColor(ref_norm,  cv2.COLOR_RGB2GRAY)
        crop_gray = cv2.cvtColor(crop_norm, cv2.COLOR_RGB2GRAY)

        orb = cv2.ORB_create(nfeatures=500)
        kp1, des1 = orb.detectAndCompute(ref_gray,  None)
        kp2, des2 = orb.detectAndCompute(crop_gray, None)

        if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
            return 0.0

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(des1, des2)

        # Lowe ratio test 대신 거리 기준 필터 (crossCheck이므로 단순 distance cutoff)
        good = [m for m in matches if m.distance < 50]
        denom = max(len(kp1), len(kp2), 1)
        score = len(good) / denom
        return float(min(1.0, score))
    except Exception as e:
        logger.debug(f"ORB 실패: {e}")
        return 0.0


def _compute_red_mask_ssim(ref_np, crop_np, sz: tuple = (64, 64)) -> float:
    """
    빨간색 픽셀 마스크 이진화 후 SSIM 비교.

    배경색이 달라도(검정 vs 흰색) 빨간 심볼 형태만 비교하므로
    누끼 레퍼런스 이미지에 강건하다.

    반환값: 0~1 (1=완전 일치), 빨간 픽셀 부족 시 -1.0 (비교 불가)
    """
    try:
        import cv2
        from skimage.metrics import structural_similarity as ssim

        def to_red_binary(np_img, target_sz):
            bgr = cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            m1 = cv2.inRange(hsv, (0,  60, 60), (12,  255, 255))
            m2 = cv2.inRange(hsv, (168, 60, 60), (180, 255, 255))
            mask = cv2.bitwise_or(m1, m2)
            # 안티앨리어싱 팽창
            kernel = np.ones((3, 3), np.uint8)
            mask = cv2.dilate(mask, kernel, iterations=1)
            resized = cv2.resize(mask, target_sz, interpolation=cv2.INTER_AREA)
            _, binary = cv2.threshold(resized, 64, 255, cv2.THRESH_BINARY)
            return binary, mask.sum() // 255  # (이진화 이미지, 원본 빨간 픽셀 수)

        import numpy as np
        ref_bin,  ref_red_cnt  = to_red_binary(ref_np,  sz)
        crop_bin, crop_red_cnt = to_red_binary(crop_np, sz)

        # 빨간 픽셀이 너무 적으면 비교 의미 없음
        ref_total  = ref_np.shape[0]  * ref_np.shape[1]
        crop_total = crop_np.shape[0] * crop_np.shape[1]
        if ref_red_cnt / max(ref_total, 1) < 0.01 or crop_red_cnt / max(crop_total, 1) < 0.01:
            logger.debug(f"[red_mask] 빨간 픽셀 부족: ref={ref_red_cnt}, crop={crop_red_cnt}")
            return -1.0  # 비교 불가 신호

        score, _ = ssim(ref_bin, crop_bin, full=True)
        logger.debug(f"[red_mask] SSIM={score:.3f}  (ref_red={ref_red_cnt}, crop_red={crop_red_cnt})")
        return float(max(0.0, score))
    except Exception as e:
        logger.debug(f"red_mask SSIM 실패: {e}")
        return -1.0


class ClaudeVisionJudge:
    """이미지 배치 기반 Claude Vision 판정 엔진"""

    def __init__(self):
        self.enabled = _cfg.CLAUDE_ENABLED
        self._client = None
        if self.enabled:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=_cfg.ANTHROPIC_API_KEY)
                logger.info(f"Claude Vision 준비: {_cfg.CLAUDE_MODEL}")
            except Exception as e:
                logger.warning(f"Claude 초기화 실패: {e}")
                self.enabled = False
        else:
            logger.info("ANTHROPIC_API_KEY 없음 → 규칙 기반 전용")

    # ── 메타데이터 규칙 판정 ─────────────────────────────────────
    def judge_metadata(self, metadata: dict, allowed_check_fn) -> list:
        from models.schemas import DetectionResult, DetectionType, VerdictType
        results = []
        sensitive = {
            "author":   "문서 작성자",
            "creator":  "문서 생성 프로그램/회사",
            "producer": "PDF 생성 프로그램",
            "subject":  "문서 주제",
            "keywords": "키워드",
        }
        for field, desc in sensitive.items():
            val = metadata.get(field, "").strip()
            if not val:
                continue
            if allowed_check_fn(val):
                results.append(DetectionResult(
                    page_number=0, detection_type=DetectionType.METADATA,
                    detected_text=f"[{field}] {val}",
                    verdict=VerdictType.ALLOWED,
                    reason=f"허용 목록 기관 ({desc})",
                    recommendation="수정 불필요",
                    confidence=0.85, source="rule"))
            else:
                results.append(DetectionResult(
                    page_number=0, detection_type=DetectionType.METADATA,
                    detected_text=f"[{field}] {val}",
                    verdict=VerdictType.VIOLATION,
                    reason=f"PDF 메타데이터 '{desc}' 필드에 제안사 식별정보 포함 가능",
                    recommendation=f"'{field}' 메타데이터 삭제 필요",
                    confidence=0.85, source="rule"))
        return results

    # ── 이미지 배치 판정 (핵심) ──────────────────────────────────
    def judge_image_batch(
        self,
        page_images: List[dict],
        logo_b64: Optional[str],
        company_dict: Optional[dict] = None,
        rule_hits: Optional[dict] = None,
        logo_symbol_b64: Optional[str] = None,
    ) -> List[dict]:
        if not page_images:
            return []
        if not self.enabled:
            return []

        # 심볼 레퍼런스 저장 (후처리 단계에서 참조)
        self._logo_symbol_b64 = logo_symbol_b64
        # company_dict 저장 (워드마크 후보 추출용)
        self._last_company_dict = company_dict

        content = []
        _cache_marked = False

        # 1. 로고 레퍼런스 첨부 (캐싱 대상)
        if logo_b64:
            content.append({
                "type": "text",
                "text": (
                    "아래는 제안사 공식 로고 레퍼런스 이미지이다.\n"
                    "이 이미지는 1차 탐지 참고용이다. 실제 위반 확정은 별도 재비교 단계에서 결정한다.\n"
                    "로고 후보 탐지 조건: 심볼 형태 + 워드마크 텍스트 + 색상 패턴 + 레이아웃 구조가 모두 명확히 동일해야 함.\n"
                    "하나라도 불명확하면 로고 후보로도 표시하지 마라.\n"
                    "발주기관(공공기관) 로고는 절대 위반 아님."
                )
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": logo_b64
                }
            })

        # 2. 회사 사전 정보 (캐싱 대상)
        if company_dict:
            direct = company_dict.get("direct_identifiers", company_dict)

            def _get(key):
                v = direct.get(key) or company_dict.get(key) or []
                return [str(x).strip() for x in v if str(x).strip()]

            lines = []
            names = _get("company_names")
            if names:
                lines.append(f"제안사명: {', '.join(names)}")
            eng = _get("english_names")
            if eng:
                lines.append(f"영문명: {', '.join(eng)}")
            abbr = _get("abbreviations")
            if abbr:
                lines.append(f"약칭: {', '.join(abbr)}")
            rep = _get("representative_names")
            if rep:
                lines.append(f"대표자: {', '.join(rep)}")
            emails = _get("emails")
            if emails:
                lines.append(f"이메일: {', '.join(emails)}")
            domains = _get("domains")
            if domains:
                lines.append(f"도메인: {', '.join(domains)}")
            brands = _get("brand_names")
            if brands:
                lines.append(f"브랜드명: {', '.join(brands)}")
            indirect = company_dict.get("indirect_identifiers", {})
            for k, label in [("color_names","고유색상"), ("solution_names","솔루션명"),
                              ("slogans","슬로건"), ("org_names","조직명"), ("service_names","서비스명")]:
                vals = [str(x).strip() for x in (indirect.get(k) or []) if str(x).strip()]
                if vals:
                    lines.append(f"{label}: {', '.join(vals)}")

            if lines:
                content.append({
                    "type": "text",
                    "text": "━━━ 제안사 식별 사전 (이 항목이 이미지에 보이면 위반 후보) ━━━\n" + "\n".join(lines),
                    "cache_control": {"type": "ephemeral"}
                })
                _cache_marked = True

        if not _cache_marked and logo_b64 and content:
            content[-1]["cache_control"] = {"type": "ephemeral"}

        # 3. 페이지별 PAGE 블록 구조로 감싸기 (페이지 간 전이 차단 핵심)
        valid_pages = [pg["page"] for pg in page_images]

        for pg in page_images:
            page_num = pg["page"]
            page_key = str(page_num)

            # [PAGE N START]
            content.append({
                "type": "text",
                "text": f"[PAGE {page_num} START]\npage_number: {page_num}"
            })

            # 텍스트 후보 탐지 결과 (확정→후보로 변경)
            if rule_hits and page_key in rule_hits and rule_hits[page_key]:
                hits = rule_hits[page_key]
                violations = [h for h in hits if h.get("judgment") == "위반"]
                cautions   = [h for h in hits if h.get("judgment") == "주의"]

                candidate_lines = [
                    f"[PAGE {page_num} 텍스트 후보] — 이미지에서 직접 확인된 경우에만 출력하라. 확인되지 않으면 출력하지 마라."
                ]
                for h in violations:
                    c = h.get('content', '')
                    t = h.get('type', '')
                    candidate_lines.append(f"  후보: page={page_num}, type={t}, content={c}, judgment=위반")
                for h in cautions:
                    c = h.get('content', '')
                    t = h.get('type', '')
                    candidate_lines.append(f"  후보: page={page_num}, type={t}, content={c}, judgment=주의")

                content.append({
                    "type": "text",
                    "text": "\n".join(candidate_lines)
                })

            # 페이지 이미지
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": pg.get("media_type", "image/jpeg"),
                    "data": pg["b64"]
                }
            })

            # [PAGE N END]
            content.append({
                "type": "text",
                "text": (
                    f"[PAGE {page_num} END]\n"
                    f"이 페이지는 위 이미지만을 근거로 독립적으로 판정하라.\n"
                    f"다른 PAGE 블록 내용 참조 금지. 특히 로고는 이 페이지 이미지에서만 판단하라."
                )
            })

        # 최종 지시
        page_list = ", ".join(str(p) for p in valid_pages)
        content.append({
            "type": "text",
            "text": (
                f"위 PAGE 블록들({page_list})을 블라인드 검증하라.\n"
                f"각 PAGE 블록은 독립적으로 판단하라. 블록 간 내용 전이 금지.\n"
                f"로고 판정은 매우 보수적으로: 4가지 조건(형태+워드마크+색상+레이아웃) 모두 충족 시만 위반.\n"
                f"JSON만 반환하라."
            )
        })

        try:
            resp = self._client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=8192,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}
                }],
                messages=[{"role": "user", "content": content}],
            )
            raw = resp.content[0].text.strip()
            usage = resp.usage
            cache_read  = getattr(usage, "cache_read_input_tokens",  0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_info  = f" [캐시 읽기:{cache_read} 저장:{cache_write}]" if (cache_read or cache_write) else ""
            logger.info(f"Claude 응답 p{valid_pages}: {len(raw)}자{cache_info}")

            items = self._parse_items(raw, valid_pages)

            # ── 로고 후처리: 공공기관 오탐 차단 + 전체/심볼 재비교 ──
            logo_symbol_b64 = getattr(self, "_logo_symbol_b64", None)
            items = self._post_process_logo(
                items, page_images, logo_b64,
                logo_symbol_b64=logo_symbol_b64,
                company_dict=company_dict,
            )
            # ── 얼굴/인물사진 오탐 후처리 (bbox 기반 이미지 재검증) ───
            items = _post_process_faces(
                items,
                page_images=page_images,
                client=self._client,
                model=_cfg.CLAUDE_MODEL if self.enabled else "",
            )

            return items
        except Exception as e:
            logger.error(f"Claude Vision 오류 p{valid_pages}: {e}")
            return []

    # ── 로고 후처리: 공공기관 필터 + 전체비교 + 심볼비교 ────────────
    def _post_process_logo(
        self,
        items: List[dict],
        page_images: List[dict],
        logo_b64: Optional[str],
        logo_symbol_b64: Optional[str] = None,
        company_dict: Optional[dict] = None,
    ) -> List[dict]:
        """
        4단계 로고 판정 파이프라인 (요구사항):

        Case A:      전체 로고 일치                              → 위반
        Case B:      전체 로고 불일치 + 심볼 일치 + 워드마크      → 위반
        Case C:      심볼만 일치 (워드마크 없음)                  → 주의
        Case D:      심볼 불일치                                  → 허용

        심볼 레퍼런스(logo_symbol_b64) 없으면:
          → _extract_symbol_from_logo()로 전체 로고에서 자동 추출
          → 추출 성공 시 Case B/C/D-Auto로 동일 흐름 수행
          → 추출 실패 시 허용 처리 (레퍼런스 부족)
        """
        if not items:
            return items

        # 페이지번호 → b64 매핑
        page_b64_map = {pg["page"]: (pg["b64"], pg.get("media_type", "image/jpeg"))
                        for pg in page_images}

        # 워드마크 후보 추출 (company_dict에서)
        wordmark_candidates: list = list(_DEFAULT_WORDMARK_CANDIDATES)
        if company_dict:
            direct = company_dict.get("direct_identifiers", company_dict)
            for key in ("company_names", "english_names", "abbreviations", "brand_names"):
                for v in (direct.get(key) or company_dict.get(key) or []):
                    v = str(v).strip()
                    if v and v.lower() not in [c.lower() for c in wordmark_candidates]:
                        wordmark_candidates.append(v)

        processed = []
        for it in items:
            dtype    = it.get("type",     "")
            content  = it.get("content",  "")
            judgment = it.get("judgment", "주의")

            # ── 공공기관 로고 오탐 차단 (최우선) ─────────────────
            if _is_logo_type(dtype) or _is_logo_type(content):
                for kw in _PUBLIC_ORG_KEYWORDS:
                    if kw.lower() in content.lower() or kw.lower() in it.get("reason", "").lower():
                        it["judgment"] = "허용"
                        it["reason"] = f"공공기관/발주기관 로고로 확인됨 ({kw}) — 제안사 로고 아님"
                        it["recommendation"] = ""
                        logger.debug(f"공공기관 로고 오탐 차단: {content} (키워드: {kw})")
                        break

            # 허용으로 이미 결정됐으면 이후 단계 스킵
            if it.get("judgment") == "허용":
                processed.append(it)
                continue

            # ── 로고/로고후보 재비교 파이프라인 ──────────────────
            if judgment in ("위반", "주의") and _is_logo_type(dtype):
                page_num = it.get("page", 0)
                b64_pair = page_b64_map.get(page_num)

                if not b64_pair:
                    # 페이지 이미지 없음 → 판정 유지
                    processed.append(it)
                    continue

                b64, mtype = b64_pair

                # bbox 파싱
                raw_bbox = it.get("bbox")
                bbox: Optional[list] = None
                if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                    try:
                        bbox = [float(v) for v in raw_bbox]
                    except (TypeError, ValueError):
                        bbox = None

                if not logo_b64 and not logo_symbol_b64:
                    # 레퍼런스 전혀 없음 → 위반→주의 강등
                    if it.get("judgment") == "위반":
                        it["judgment"] = "주의"
                        it["reason"] = "[로고 레퍼런스 없음] 레퍼런스 없이 위반 확정 불가 → 주의"
                    processed.append(it)
                    continue

                # ── Case A: 전체 로고 비교 ─────────────────────
                full_match = False
                if logo_b64:
                    full_match = _verify_logo_candidate(b64, logo_b64, bbox, mtype)

                if full_match:
                    it["judgment"] = "위반"
                    it["reason"] = (
                        f"[Case A] 전체 로고 레퍼런스 일치 — 위반 확정"
                    )
                    logger.info(f"Case A: 전체 로고 일치 → 위반 확정: p{page_num} '{content}'")
                    processed.append(it)
                    continue

                # ── Case B/C/D: 심볼 비교 단계 ──────────────────
                if logo_symbol_b64:
                    sym_match = _verify_symbol_candidate(b64, logo_symbol_b64, bbox)

                    if sym_match:
                        # 워드마크 확인
                        ocr_svc = None
                        try:
                            from services.ocr_service import get_ocr
                            ocr_svc = get_ocr()
                        except Exception:
                            pass

                        has_wm = _has_wordmark_nearby(
                            b64, bbox,
                            wordmark_candidates=wordmark_candidates,
                            ocr_service=ocr_svc,
                        )

                        if has_wm:
                            # Case B: 심볼 + 워드마크 → 위반
                            it["judgment"] = "위반"
                            it["reason"] = (
                                f"[Case B] 전체 로고 불일치 but 심볼 일치 + 워드마크 검출 "
                                f"— 제안사 로고 위반 확정"
                            )
                            logger.info(
                                f"Case B: 심볼+워드마크 일치 → 위반: p{page_num} '{content}'"
                            )
                        else:
                            # Case C: 심볼만 일치 → 주의
                            it["judgment"] = "주의"
                            it["reason"] = (
                                f"[Case C] 심볼 유사 but 워드마크 미검출 "
                                f"— 단순 그래픽 오탐 가능성, 수동 확인 필요"
                            )
                            it["recommendation"] = (
                                "심볼과 유사한 그래픽 요소 발견 — 워드마크 텍스트가 없으면 오탐 가능"
                            )
                            logger.info(
                                f"Case C: 심볼만 일치(워드마크 없음) → 주의: p{page_num} '{content}'"
                            )
                    else:
                        # Case D: 심볼 불일치 → 허용
                        original_judgment = it.get("judgment", "주의")
                        it["judgment"] = "허용"
                        it["reason"] = (
                            f"[Case D] 전체 로고 불일치 + 심볼 불일치 "
                            f"(Claude 1차: {original_judgment}) — 허용 처리"
                        )
                        it["recommendation"] = ""
                        logger.info(
                            f"Case D: 심볼 불일치 → 허용: p{page_num} '{content}'"
                        )
                else:
                    # ── 심볼 레퍼런스 없음 → 전체 로고에서 자동 추출 후 재시도 ──
                    auto_sym_b64 = _extract_symbol_from_logo(logo_b64) if logo_b64 else None

                    if auto_sym_b64:
                        # 자동 추출 성공 → 심볼 비교 수행 (Case B/C/D)
                        logger.info(
                            f"심볼 자동 추출 성공 → 재비교 수행: p{page_num} '{content}'"
                        )
                        sym_match = _verify_symbol_candidate(b64, auto_sym_b64, bbox)

                        if sym_match:
                            ocr_svc = None
                            try:
                                from services.ocr_service import get_ocr
                                ocr_svc = get_ocr()
                            except Exception:
                                pass

                            has_wm = _has_wordmark_nearby(
                                b64, bbox,
                                wordmark_candidates=wordmark_candidates,
                                ocr_service=ocr_svc,
                            )

                            if has_wm:
                                # Case B (자동 추출): 심볼 + 워드마크 → 위반
                                it["judgment"] = "위반"
                                it["reason"] = (
                                    f"[Case B-Auto] 전체 로고 불일치 but "
                                    f"자동 추출 심볼 일치 + 워드마크 검출 — 위반 확정"
                                )
                                logger.info(
                                    f"Case B-Auto: 자동추출 심볼+워드마크 → 위반: "
                                    f"p{page_num} '{content}'"
                                )
                            else:
                                # Case C (자동 추출): 심볼만 일치 → 주의
                                it["judgment"] = "주의"
                                it["reason"] = (
                                    f"[Case C-Auto] 자동 추출 심볼 유사 but 워드마크 미검출 "
                                    f"— 수동 확인 필요"
                                )
                                it["recommendation"] = (
                                    "자동 추출 심볼과 유사 — 워드마크 미검출로 주의 처리, "
                                    "수동 검토 권장"
                                )
                                logger.info(
                                    f"Case C-Auto: 자동추출 심볼만 일치(워드마크 없음) → 주의: "
                                    f"p{page_num} '{content}'"
                                )
                        else:
                            # Case D (자동 추출): 심볼 불일치 → 허용
                            original_judgment = it.get("judgment", "주의")
                            it["judgment"] = "허용"
                            it["reason"] = (
                                f"[Case D-Auto] 전체 로고 불일치 + 자동 추출 심볼 불일치 "
                                f"(Claude 1차: {original_judgment}) — 허용 처리"
                            )
                            it["recommendation"] = ""
                            logger.info(
                                f"Case D-Auto: 자동추출 심볼 불일치 → 허용: "
                                f"p{page_num} '{content}'"
                            )
                    else:
                        # 자동 추출도 실패 → 전체 레퍼런스 불일치 그대로 허용
                        original_judgment = it.get("judgment", "주의")
                        it["judgment"] = "허용"
                        it["reason"] = (
                            f"[로고 재비교 불일치] Claude 1차: {original_judgment}이었으나 "
                            f"전체 레퍼런스 불일치, 심볼 자동 추출 실패 → 허용 처리"
                        )
                        it["recommendation"] = ""
                        logger.info(
                            f"전체 불일치+심볼 자동 추출 실패 → 허용: p{page_num} '{content}'"
                        )

            processed.append(it)

        return processed

    # ── 응답 파싱 + 검증 강화 ────────────────────────────────────
    def _parse_items(self, raw: str, valid_pages: list = None) -> List[dict]:
        """JSON { items: [...] } 파싱 + 출력 검증"""
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        s = raw.find("{")
        e = raw.rfind("}")
        if s == -1 or e == -1:
            logger.warning(f"JSON 객체 없음: {raw[:200]}")
            return []
        try:
            obj = json.loads(raw[s:e+1])
            items = obj.get("items", [])
            cleaned = []
            valid_judgments = {"위반", "주의", "허용"}

            for it in items:
                if not isinstance(it, dict):
                    continue

                # 필수 필드 보정
                it.setdefault("page", "?")
                it.setdefault("type", "기타")
                it.setdefault("content", "")
                it.setdefault("judgment", "주의")
                it.setdefault("reason", "")
                it.setdefault("recommendation", "")
                # bbox: 로고 재비교용 — 없으면 None으로 통일
                if "bbox" not in it:
                    it["bbox"] = None
                elif it["bbox"] is not None:
                    # 유효한 4-원소 리스트인지 검증
                    try:
                        it["bbox"] = [float(v) for v in it["bbox"]] if len(it["bbox"]) == 4 else None
                    except (TypeError, ValueError):
                        it["bbox"] = None

                # 검증 1: page가 실제 배치 범위 안에 있는지
                if valid_pages:
                    try:
                        p = int(it["page"])
                    except (ValueError, TypeError):
                        logger.warning(f"[파서] 잘못된 page값 제외: {it.get('page')}")
                        continue
                    if p not in valid_pages:
                        logger.warning(f"[파서] 배치 범위 밖 page 제외: p{p} (배치: {valid_pages})")
                        continue
                    it["page"] = p

                # 검증 2: judgment가 유효한 값인지
                if it["judgment"] not in valid_judgments:
                    it["judgment"] = "주의"

                # 검증 3: content가 비어있는 항목 제거
                if not str(it["content"]).strip():
                    continue

                cleaned.append(it)

            return cleaned
        except json.JSONDecodeError as ex:
            logger.warning(f"JSON 파싱 실패: {ex} | {raw[:300]}")
            return []


# ── 헬퍼: 로고 관련 타입/컨텐츠 여부 판별 ───────────────────────
def _is_logo_type(text: str) -> bool:
    """type 또는 content가 로고 관련인지 확인"""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in ("로고", "logo", "ci", "bi", "브랜드", "brand", "심볼", "symbol", "로고후보"))


# ── 얼굴/인물사진 후처리 필터 ────────────────────────────────────

# 얼굴/인물 타입 키워드 (이 타입이면 필터 대상)
_FACE_DTYPE_KW: tuple = ("인물", "사진", "얼굴", "face", "photo", "인물사진", "사람")

# ★ 실제 사진 확정 키워드 (이것이 있을 때만 위반 유지)
# 반드시 카메라 촬영된 실사 사진임을 나타내는 표현
_REAL_PHOTO_KW: tuple = (
    "피부색", "피부 질감", "skin",
    "이목구비",
    "눈코입", "눈·코·입",
    "사진 질감", "photo texture",
    "실사", "real photo",
    "카메라", "촬영",
    "프로필 사진", "profile photo",
    "얼굴 사진",
    "인물 사진",
)

# ★ 그래픽/아이콘 확정 키워드 (하나라도 있으면 반드시 허용)
_GRAPHIC_KW: tuple = (
    "실루엣", "silhouette",
    "아이콘", "icon",
    "픽토그램", "pictogram",
    "벡터", "vector",
    "일러스트", "illust",
    "캐릭터", "character",
    "다이어그램", "diagram",
    "단색", "monochrome",
    "스케치", "sketch",
    "그래픽", "graphic",
    "이모지", "emoji",
    "아이콘 형태", "icon style",
    "사람 아이콘", "person icon",
    "인물 아이콘",
    "연구자 아이콘", "직원 아이콘", "사용자 아이콘",
    "얼굴 없음", "얼굴 디테일 없음",
    "눈코입 없음",
)


# ── 인물 사진 bbox crop 후 이미지 재분류 ─────────────────────────────────────
_FACE_VERIFY_PROMPT = """이 이미지 crop을 보고 아래 세 가지 중 하나로만 답하라.

판단 기준:
  real_photo    → 피부 질감이 보이고, 눈·코·입 이목구비가 식별되며, 카메라로 촬영된 실사 인물 사진임이 90% 이상 확실한 경우
  icon_or_silhouette → 단색 실루엣, 사람 아이콘, 픽토그램, 벡터 일러스트, 캐릭터, 얼굴 디테일 없는 그래픽 중 하나라도 해당하는 경우
  unknown       → 이미지가 너무 작거나 흐릿해 판단 불가, 또는 위 두 가지 모두에 해당하지 않는 경우

반드시 아래 JSON 형식으로만 답하라:
{"result": "real_photo" 또는 "icon_or_silhouette" 또는 "unknown", "reason": "간단한 판단 근거"}"""


def _verify_face_candidate(
    page_b64: str,
    bbox: Optional[list],
    client=None,
    model: str = "",
) -> str:
    """
    인물 사진 후보 bbox 영역을 crop 후 재분류.

    판정 순서 (색상 휴리스틱 선제 → CV 탐지 → Claude Vision 폴백):
      1단계: 색상/채도 휴리스틱 (단색·벡터 즉시 허용)
             → 단색/실루엣(unique≤12, sat_std<25) → icon_or_silhouette 즉시 반환
             → 벡터/플랫(unique≤20, sat_std<35) → icon_or_silhouette 즉시 반환
             (MediaPipe가 단색 원·사각형을 얼굴로 오탐하는 문제 방지)
      2단계: MediaPipe BlazeFace + OpenCV Haar (결정론적)
             → 얼굴 검출 (conf ≥ 0.5) → real_photo 즉시 반환
             → 얼굴 없음 → 피부색 휴리스틱으로 진행
      3단계: 피부색 휴리스틱 (비정면·작은 얼굴 보완)
             → skin_ratio ≥ 0.06 AND sat_std > 45 → real_photo
      4단계: Claude Vision (불명확 케이스만)
             → real_photo / icon_or_silhouette / unknown

    반환값:
      "real_photo"         — 실제 얼굴/카메라 사진 확인
      "icon_or_silhouette" — 아이콘/실루엣/그래픽
      "unknown"            — 판단 불충분
    """
    try:
        crop_img = _extract_crop(page_b64, bbox, pad=8)
        if crop_img is None:
            logger.debug("[face_verify] crop 실패 → unknown")
            return "unknown"

        # 공유 분석 변수 초기화
        _fg_skin_ratio = 0.0   # 전경 기준 피부색 비율
        _fg_sat_std    = 0.0   # 전경 기준 채도 표준편차
        _heuristic_done = False

        # ══ 1단계: 색상/채도 휴리스틱 (선제 필터 — 단색·아이콘 즉시 허용) ══
        # 전략:
        #   A. 코너 평균 → 배경색 추정 → 전경 픽셀 분리
        #   B. 전경 기준 피부색 비율로 아이콘/실사 판별
        #      skin_fg < 0.10  → 피부색 거의 없음  → icon_or_silhouette
        #      skin_fg ≥ 0.15  → 피부색 풍부       → real_photo  (3단계 생략)
        #      0.10 ~ 0.15    → 애매               → CV 탐지로 진행
        try:
            import numpy as np
            import cv2
            from PIL import Image as _PIL

            thumb = crop_img.resize((64, 64), _PIL.LANCZOS).convert("RGB")
            arr   = np.array(thumb)

            # ── A: 배경색 추정 (코너 4픽셀 평균) ──────────────────────
            corners  = [arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]]
            bg_color = np.mean(corners, axis=0)
            diff     = np.sqrt(np.sum((arr.astype(np.float32) - bg_color) ** 2, axis=2))
            fg_mask  = diff > 20      # 배경과 20 이상 차이 = 전경
            fg_pixels = arr[fg_mask]
            fg_count  = int(fg_mask.sum())

            # ── B: 전경 픽셀 채도 분석 ─────────────────────────────────
            hsv     = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
            fg_sat  = hsv[:, :, 1][fg_mask]
            _fg_sat_std = float(fg_sat.std()) if fg_count > 0 else 0.0

            # ── C: 단색/실루엣 조기 판별 ───────────────────────────────
            if fg_count > 0:
                quantized = (fg_pixels // 32).astype(np.int32)
                unique_fg = len(set(map(tuple, quantized.tolist())))

                if unique_fg <= 10 and _fg_sat_std < 30:
                    logger.info(
                        f"[face_verify] 단색/실루엣 → icon_or_silhouette "
                        f"(unique_fg={unique_fg}, fg_sat_std={_fg_sat_std:.1f})"
                    )
                    return "icon_or_silhouette"

            # ── D: 전경 기준 피부색 비율 계산 ──────────────────────────
            if fg_count > 0:
                r, g, b = fg_pixels[:, 0], fg_pixels[:, 1], fg_pixels[:, 2]
                skin = (
                    (r > 150) & (g > 100) & (b > 80) &
                    ((r.astype(np.int16) - g.astype(np.int16)) > 10) &
                    ((g.astype(np.int16) - b.astype(np.int16)) > 5)
                )
                _fg_skin_ratio = float(skin.sum()) / max(fg_count, 1)

            logger.debug(
                f"[face_verify] fg={fg_count}px, "
                f"skin_fg={_fg_skin_ratio:.3f}, fg_sat_std={_fg_sat_std:.1f}"
            )

            # 피부색 거의 없음 → 아이콘/그래픽 (실사 불필요)
            if _fg_skin_ratio < 0.20:
                logger.info(
                    f"[face_verify] 피부색 부족 → icon_or_silhouette "
                    f"(skin_fg={_fg_skin_ratio:.3f})"
                )
                return "icon_or_silhouette"

            # 피부색 풍부 → 실사 (CV 탐지 없이 즉시)
            if _fg_skin_ratio >= 0.20 and _fg_sat_std > 30:
                logger.info(
                    f"[face_verify] 피부색 풍부 → real_photo "
                    f"(skin_fg={_fg_skin_ratio:.3f}, fg_sat_std={_fg_sat_std:.1f})"
                )
                return "real_photo"

            # ≥ 0.20 이지만 sat_std 낮음 → 애매 → CV 탐지 진행
            _heuristic_done = True
            logger.debug(
                f"[face_verify] 피부색 있으나 채도 낮음 ({_fg_skin_ratio:.3f}, sat={_fg_sat_std:.1f}) → CV 탐지 진행"
            )

        except Exception as _he:
            logger.debug(f"[face_verify] 색상 휴리스틱 실패 (무시 후 진행): {_he}")

        # ══ 2단계: Computer Vision 얼굴 탐지 (결정론적) ════════════════
        face_detected = _detect_real_face(crop_img)
        if face_detected:
            logger.info(
                "[face_verify] CV 얼굴 검출 → real_photo "
                f"(crop={crop_img.width}×{crop_img.height})"
            )
            return "real_photo"

        # ══ 3단계: 피부색 휴리스틱 보완 (CV 미탐 + 전경 피부색 있는 경우) ════
        # 1단계에서 0.10~0.15 범위였고 CV도 미탐 → 경계값 처리
        # 임계값을 높게 유지: skin_fg ≥ 0.20 + fg_sat_std > 45
        # (벡터 일러스트 살구색이 0.14~0.15 수준임을 고려)
        try:
            if _fg_skin_ratio >= 0.20 and _fg_sat_std > 45:
                logger.info(
                    f"[face_verify] 피부색 보완 → real_photo "
                    f"(skin_fg={_fg_skin_ratio:.3f}, fg_sat_std={_fg_sat_std:.1f})"
                )
                return "real_photo"
        except Exception as _se:
            logger.debug(f"[face_verify] 피부색 보완 실패: {_se}")

        # ══ 4단계: Claude Vision (불명확 케이스만) ═══════════════════════
        if client is not None:
            try:
                buf = io.BytesIO()
                crop_img.save(buf, format="JPEG", quality=85)
                crop_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

                _model = model or ""
                try:
                    import core.config as _cfg2
                    _model = _model or _cfg2.CLAUDE_MODEL
                except Exception:
                    _model = _model or "claude-3-5-haiku-20241022"

                resp = client.messages.create(
                    model=_model,
                    max_tokens=256,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": crop_b64,
                                }
                            },
                            {"type": "text", "text": _FACE_VERIFY_PROMPT},
                        ]
                    }]
                )
                raw = resp.content[0].text.strip()
                raw_clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
                s = raw_clean.find("{")
                e = raw_clean.rfind("}")
                if s != -1 and e != -1:
                    obj = json.loads(raw_clean[s:e+1])
                    result = obj.get("result", "unknown")
                    reason = obj.get("reason", "")
                    if result in ("real_photo", "icon_or_silhouette", "unknown"):
                        logger.info(f"[face_verify] Claude 재판정 → {result}: {reason[:60]}")
                        return result
                logger.debug(f"[face_verify] Claude 응답 파싱 실패: {raw[:100]}")
            except Exception as _ce:
                logger.warning(f"[face_verify] Claude 재판정 실패: {_ce}")

        logger.debug("[face_verify] 판정 불가 → unknown")
        return "unknown"

    except Exception as e:
        logger.warning(f"[face_verify] 전체 오류: {e} → unknown")
        return "unknown"



def _post_process_faces(
    items: List[dict],
    page_images: Optional[List[dict]] = None,
    client=None,
    model: str = "",
) -> List[dict]:
    """
    인물사진 타입 항목 후처리 — bbox 기반 이미지 재검증 포함.

    수정된 파이프라인:
      Claude Vision → 인물 후보 + bbox
      → bbox crop → _verify_face_candidate() 이미지 재분류
      → real_photo       → 위반 유지
      → icon_or_silhouette → 허용 ("아이콘/실루엣/일러스트로 확인되어 허용 처리")
      → unknown          → 허용 ("실제 촬영 사진으로 확인되지 않아 허용 처리")

    키워드 필터(_REAL_PHOTO_KW, _GRAPHIC_KW)는 2차 힌트로만 사용:
      - 이미지 재검증이 가능한 경우 → 이미지 판정 우선
      - page_images 없거나 bbox 없는 경우 → 키워드 필터 폴백

    인자:
      items       : Claude Vision이 반환한 아이템 리스트
      page_images : [{"page": N, "b64": ..., "media_type": ...}, ...]
                    없으면 키워드 필터만 적용 (하위 호환)
      client      : anthropic.Anthropic 인스턴스 (None이면 휴리스틱만)
      model       : Claude 모델명
    """
    # 페이지번호 → b64 매핑
    page_b64_map: dict = {}
    if page_images:
        for pg in page_images:
            page_b64_map[pg["page"]] = pg["b64"]

    out = []
    for it in items:
        dtype    = (it.get("type")    or "").lower()
        content  = (it.get("content") or "").lower()
        reason   = (it.get("reason")  or "").lower()
        judgment = it.get("judgment", "주의")

        # 얼굴/인물 타입 아니면 그대로 통과
        is_face = any(kw in dtype for kw in _FACE_DTYPE_KW)
        if not is_face:
            out.append(it)
            continue

        # 이미 허용 → 통과
        if judgment == "허용":
            out.append(it)
            continue

        # ── 1단계: 이미지 재검증 (bbox + page_images 모두 있는 경우) ──────────
        page_num = it.get("page")
        raw_bbox = it.get("bbox")
        bbox: Optional[list] = None
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                bbox = [float(v) for v in raw_bbox]
            except (TypeError, ValueError):
                bbox = None

        page_b64 = page_b64_map.get(page_num) if page_num is not None else None

        if page_b64 is not None:
            # bbox가 없어도 페이지 전체로 시도 (None bbox → _extract_crop fallback)
            face_class = _verify_face_candidate(
                page_b64, bbox, client=client, model=model
            )

            if face_class == "real_photo":
                # 이미지 재검증 결과: 실제 사진 → 위반 유지
                it = dict(it)
                it["_face_reverified"] = True  # ★ server_pipeline 중복 처리 방지
                logger.info(
                    f"[face_verify] 실제 사진 확인 → 위반 유지: "
                    f"p{page_num} '{it.get('content', '')[:40]}'"
                )
                out.append(it)
                continue

            elif face_class == "icon_or_silhouette":
                # 이미지 재검증 결과: 아이콘/실루엣 → 허용
                it = dict(it)
                it["judgment"]       = "허용"
                it["reason"]         = "아이콘/실루엣/일러스트로 확인되어 허용 처리 (이미지 재검증)"
                it["recommendation"] = ""
                it["_face_reverified"] = True  # ★ server_pipeline 중복 처리 방지
                logger.info(
                    f"[face_verify] 아이콘/실루엣 확인 → 허용: "
                    f"p{page_num} '{it.get('content', '')[:40]}'"
                )
                out.append(it)
                continue

            else:  # unknown
                # 이미지 재검증 결과: 불충분
                # ★ unknown이면 키워드 폴백으로 2차 판단
                #   그래픽 키워드 있으면 허용, 없으면 위반 유지 (안전 방향)
                combined_kw = dtype + " " + content + " " + reason
                if any(kw in combined_kw for kw in _GRAPHIC_KW):
                    it = dict(it)
                    it["judgment"]       = "허용"
                    it["reason"]         = "그래픽/아이콘으로 확인되어 허용 처리 (이미지 재검증 후 키워드 확인)"
                    it["recommendation"] = ""
                    it["_face_reverified"] = True  # ★ server_pipeline 중복 처리 방지
                    logger.info(
                        f"[face_verify] unknown+그래픽키워드 → 허용: "
                        f"p{page_num} '{it.get('content', '')[:40]}'"
                    )
                else:
                    # unknown + 그래픽 키워드 없음 → 위반 유지 (실사 가능성)
                    it = dict(it)
                    it["_face_reverified"] = True  # ★ server_pipeline 중복 처리 방지
                    logger.info(
                        f"[face_verify] unknown → 위반 유지 (실사 가능성): "
                        f"p{page_num} '{it.get('content', '')[:40]}'"
                    )
                out.append(it)
                continue

        # ── 2단계: 키워드 폴백 (page_images 없거나 page 불일치) ──────────────
        combined = dtype + " " + content + " " + reason

        # ① 그래픽/아이콘 키워드 존재 → 무조건 허용
        if any(kw in combined for kw in _GRAPHIC_KW):
            it = dict(it)
            it["judgment"]       = "허용"
            it["reason"]         = "그래픽/아이콘/일러스트로 확인되어 허용 처리 (실제 사진 아님)"
            it["recommendation"] = ""
            logger.debug(f"얼굴 오탐 허용: '{it.get('content', '')}' (그래픽 키워드 검출)")
            out.append(it)
            continue

        # ② 실제 사진 키워드 존재 → 위반 유지
        if any(kw in combined for kw in _REAL_PHOTO_KW):
            logger.debug(f"실제 사진 확인 → 위반 유지: '{it.get('content', '')}' (실사 키워드 검출)")
            out.append(it)
            continue

        # ③ 불명확 (어느 쪽도 아님)
        #   page_images 없는 폴백이므로 기본값 위반 유지 (안전 방향)
        logger.debug(f"얼굴 불명확 → 위반 유지: '{it.get('content', '')}' (키워드 없음, page_images 없음)")
        out.append(it)
    return out


# ── 싱글톤 ───────────────────────────────────────────────────────
_inst: ClaudeVisionJudge | None = None

def get_claude_judge() -> ClaudeVisionJudge:
    global _inst
    if _inst is None:
        _inst = ClaudeVisionJudge()
    return _inst

def _reset_judge():
    global _inst
    _inst = None
    return get_claude_judge()
