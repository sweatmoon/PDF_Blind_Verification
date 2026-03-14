"""
Claude Vision 판정 엔진 v5
- PAGE 블록 구조로 페이지 간 전이 완전 차단
- rule_hits 확정→후보 교차검증 방식
- 전체 이름 일치 기준 적용
- 이름+직책 문맥 조건 강화
- 로고 판정 4단계 보수적 정책:
    1단계 Claude Vision → 로고 후보 탐지 + bbox 반환
    2단계 bbox 기반 정확 crop (고정 영역 crop 제거)
    3단계 SSIM + ORB feature match 이중 비교
    4단계 threshold 0.70 이상 시만 위반 확정
- 발주기관/공공기관 로고 오탐 방지
- 강조 그래픽 오탐 방지
- 우측하단 편향 제거
- _parse_items() 출력 검증 강화
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
【실제 인물 사진 → 위반】
- 피부색 + 눈·코·입 이목구비가 명확히 보이는 실사 사진
- 상반신/얼굴 사진, 명함 사진, 프로필 사진 형태
- 여러 인물의 얼굴 사진 배열
→ 위 조건을 80% 이상 확신할 때만 【위반】

【위반이 아닌 경우 — 절대 위반 처리 금지】
- 단색 실루엣, 픽토그램, 아이콘, 일러스트, 만화체
- 시스템 다이어그램·플로우차트 속 사람 아이콘
- 얼굴이 흐릿하거나 식별 불가한 이미지
- 먼 거리, 뒷모습, 작은 썸네일로 얼굴 식별 불가
- AI 생성 일러스트 스타일
판단 기준: "실제 사람의 얼굴(피부색+이목구비)을 80% 이상 확신하는가?" → NO이면 반드시 【허용】

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
- bbox를 특정할 수 없으면 null로 설정
- 로고/로고후보 탐지 시 반드시 bbox를 포함하라 — 재비교에 사용됨
- 문제 없는 페이지는 포함하지 않아도 된다
- 한 페이지에 위반 요소 N개면 items 배열에 N개 항목
- 로고 후보(1차 탐지)는 type="로고후보"로 표시하고 judgment="주의"로 설정
- 최종 위반 확정은 코드에서 bbox crop 후 SSIM/ORB 재비교로 결정"""


# ── threshold ────────────────────────────────────────────────────
_LOGO_SIM_THRESHOLD = 0.70  # SSIM 또는 ORB 매칭 기준 (0.70 이상 → 위반 확정)


# ── 로고 후보 재비교: bbox crop → SSIM + ORB 이중 비교 ────────────
def _verify_logo_candidate(
    page_b64: str,
    logo_ref_b64: str,
    bbox: Optional[list] = None,
    page_media_type: str = "image/jpeg",
) -> bool:
    """
    Claude Vision이 반환한 bbox 영역을 crop 후 레퍼런스 로고와
    SSIM + ORB feature match 이중 비교.

    bbox: [x1, y1, x2, y2] 픽셀 좌표 (Claude 반환값)
          None이면 우측하단 fallback crop 사용
    반환값: True = 로고 일치 (위반 확정), False = 불일치 (허용)
    """
    try:
        from PIL import Image
        import numpy as np
        import cv2

        # ── 이미지 디코딩 ──────────────────────────────────────────
        page_bytes = base64.b64decode(page_b64)
        page_img   = Image.open(io.BytesIO(page_bytes)).convert("RGB")
        pw, ph     = page_img.width, page_img.height

        ref_bytes  = base64.b64decode(logo_ref_b64)
        ref_img    = Image.open(io.BytesIO(ref_bytes)).convert("RGB")

        # ── crop 영역 결정 ────────────────────────────────────────
        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            # 범위 클리핑
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(pw, x2), min(ph, y2)
            # 너무 작은 bbox는 패딩 확장 (최소 32×32)
            pad = 10
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(pw, x2 + pad), min(ph, y2 + pad)
            crop_img = page_img.crop((x1, y1, x2, y2))
            logger.debug(f"bbox crop: ({x1},{y1},{x2},{y2}) / 페이지 {pw}×{ph}")
        else:
            # bbox 없음 → 우측하단 fallback (보수적으로 허용 처리)
            logger.debug("bbox 없음 → fallback crop (우측하단 28%×22%)")
            x0 = int(pw * 0.72)
            y0 = int(ph * 0.78)
            crop_img = page_img.crop((x0, y0, pw, ph))

        # 비교 크기 통일
        cmp_size = (128, 64)
        crop_resized = crop_img.resize(cmp_size, Image.LANCZOS)
        ref_resized  = ref_img.resize(cmp_size, Image.LANCZOS)

        crop_np = np.array(crop_resized)
        ref_np  = np.array(ref_resized)

        # ── 1단계: SSIM 비교 ──────────────────────────────────────
        ssim_score = _compute_ssim(ref_np, crop_np)
        logger.debug(f"SSIM={ssim_score:.3f}")

        if ssim_score >= _LOGO_SIM_THRESHOLD:
            logger.info(f"로고 재비교 SSIM 일치={ssim_score:.3f} → 위반 확정")
            return True

        # ── 2단계: ORB feature match ──────────────────────────────
        orb_score = _compute_orb(ref_np, crop_np)
        logger.debug(f"ORB={orb_score:.3f}")

        result = orb_score >= _LOGO_SIM_THRESHOLD
        logger.info(
            f"로고 재비교 SSIM={ssim_score:.3f} ORB={orb_score:.3f} "
            f"→ {'위반 확정' if result else '허용'}"
        )
        return result

    except ImportError as e:
        logger.warning(f"로고 재비교 라이브러리 미설치: {e} → 허용 처리")
        return False
    except Exception as e:
        logger.warning(f"로고 재비교 실패: {e} → 허용 처리")
        return False


def _compute_ssim(ref_np, crop_np) -> float:
    """Grayscale SSIM 유사도 (0~1)"""
    try:
        from skimage.metrics import structural_similarity as ssim
        import cv2
        ref_gray  = cv2.cvtColor(ref_np,  cv2.COLOR_RGB2GRAY)
        crop_gray = cv2.cvtColor(crop_np, cv2.COLOR_RGB2GRAY)
        score, _ = ssim(ref_gray, crop_gray, full=True)
        return float(max(0.0, score))
    except Exception as e:
        logger.debug(f"SSIM 실패: {e}")
        return 0.0


def _compute_orb(ref_np, crop_np) -> float:
    """
    ORB feature match 기반 유사도 (0~1).
    매칭 비율 = good_matches / max(kp_ref, kp_crop)
    """
    try:
        import cv2
        ref_gray  = cv2.cvtColor(ref_np,  cv2.COLOR_RGB2GRAY)
        crop_gray = cv2.cvtColor(crop_np, cv2.COLOR_RGB2GRAY)

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
    ) -> List[dict]:
        if not page_images:
            return []
        if not self.enabled:
            return []

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

            # ── 로고 후처리: 공공기관 오탐 차단 + 재비교 로직 ──
            items = self._post_process_logo(items, page_images, logo_b64)

            return items
        except Exception as e:
            logger.error(f"Claude Vision 오류 p{valid_pages}: {e}")
            return []

    # ── 로고 후처리: 공공기관 필터 + 재비교 ─────────────────────
    def _post_process_logo(
        self,
        items: List[dict],
        page_images: List[dict],
        logo_b64: Optional[str],
    ) -> List[dict]:
        """
        1. 공공기관 로고 오탐 → 허용으로 변경
        2. 로고 위반/로고후보 → crop 재비교 → 불일치 시 허용 강등
        """
        if not items:
            return items

        # 페이지번호 → b64 매핑
        page_b64_map = {pg["page"]: (pg["b64"], pg.get("media_type", "image/jpeg"))
                        for pg in page_images}

        processed = []
        for it in items:
            dtype = it.get("type", "")
            content = it.get("content", "")
            judgment = it.get("judgment", "주의")

            # ── 공공기관 로고 오탐 차단 ────────────────────────
            if _is_logo_type(dtype) or _is_logo_type(content):
                for kw in _PUBLIC_ORG_KEYWORDS:
                    if kw.lower() in content.lower() or kw.lower() in it.get("reason", "").lower():
                        it["judgment"] = "허용"
                        it["reason"] = f"공공기관/발주기관 로고로 확인됨 ({kw}) — 제안사 로고 아님"
                        it["recommendation"] = ""
                        logger.debug(f"공공기관 로고 오탐 차단: {content} (키워드: {kw})")
                        break

            # ── 로고 후보/위반 → bbox crop 재비교 ──────────────
            if judgment in ("위반", "주의") and _is_logo_type(dtype) and it.get("judgment") != "허용":
                page_num = it.get("page", 0)
                if logo_b64 and page_num in page_b64_map:
                    b64, mtype = page_b64_map[page_num]
                    # Claude가 반환한 bbox 파싱
                    raw_bbox = it.get("bbox")
                    bbox = None
                    if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
                        try:
                            bbox = [float(v) for v in raw_bbox]
                        except (TypeError, ValueError):
                            bbox = None
                    matched = _verify_logo_candidate(b64, logo_b64, bbox, mtype)
                    if not matched:
                        # 재비교 불일치 → 허용 강등
                        original_judgment = it.get("judgment", "주의")
                        it["judgment"] = "허용"
                        it["reason"] = (
                            f"[로고 재비교 불일치] Claude 1차 탐지: {original_judgment}이었으나 "
                            f"레퍼런스 로고와 crop 재비교 결과 불일치 → 허용 처리"
                        )
                        it["recommendation"] = ""
                        logger.info(f"로고 재비교 불일치 → 허용: p{page_num} '{content}'")
                    else:
                        logger.info(f"로고 재비교 일치 → 위반 확정: p{page_num} '{content}'")
                elif not logo_b64:
                    # 레퍼런스 없으면 위반→주의 강등 (기존 정책 유지)
                    if it.get("judgment") == "위반":
                        it["judgment"] = "주의"
                        it["reason"] = "[로고 레퍼런스 없음] 레퍼런스 없이 위반 확정 불가 → 주의"

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
