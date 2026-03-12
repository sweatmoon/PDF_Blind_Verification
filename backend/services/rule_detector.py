"""
규칙 기반 탐지 서비스
– 정규식 패턴 + 관리자 사전 매칭
– 결과에 판정 근거·수정 권고 포함
"""
from __future__ import annotations
import re
from typing import List
from models.schemas import DetectionResult, DetectionType, VerdictType
from core.config import get_logger, load_dict

logger = get_logger("rule_detector")

# ── 정규식 패턴 ────────────────────────────────────────────────
_P = {
    "email": re.compile(
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b", re.I),
    # URL: 끝을 한글/따옴표/쉼표에서 중단
    "url": re.compile(
        r"https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{5,}"
        r"|www\.[a-zA-Z0-9\-]{2,}\.[a-zA-Z]{2,}(?:[a-zA-Z0-9\-._/?#=&%+]*[a-zA-Z0-9/])?(?=[\s,'\"]|$)",
        re.I),
    "domain": re.compile(
        r"(?<![/@\w])\b[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
        r"\.(?:com|co\.kr|or\.kr|go\.kr|net|org|io|kr)\b", re.I),
    "biz_no": re.compile(r"\b\d{3}-\d{2}-\d{5}\b"),
    "corp": re.compile(
        r"[㈜㈔]\s*[\w가-힣]+|[\w가-힣]+"
        r"(?:주식회사|유한회사|합명회사|합자회사|협동조합|유한책임회사)\b"),
    # 전화번호: 02/지방번호 뒤에 반드시 구분자(-/공백) 있어야 매칭 (02205-5205 같은 코드 제외)
    "phone": re.compile(
        r'(?<!\d)'
        r'(?:02[-\s]\d{3,4}[-\s]\d{4}|0[3-9]\d[-\s]\d{3,4}[-\s]\d{4})'
        r'(?!\d)'
    ),
}


class RuleDetector:
    def __init__(self):
        self.dictionary = load_dict()

    def reload(self):
        self.dictionary = load_dict()

    # ── 메인 탐지 ─────────────────────────────────────────────
    def detect(self, text: str, page: int) -> List[DetectionResult]:
        if not text: return []
        results: List[DetectionResult] = []
        seen:    set[tuple] = set()

        def add(r: DetectionResult):
            k = (r.page_number, r.detected_text.strip()[:80], r.detection_type)
            if k not in seen:
                seen.add(k)
                results.append(r)

        for r in self._emails(text, page):        add(r)
        for r in self._urls(text, page):          add(r)
        for r in self._biz_numbers(text, page):   add(r)
        for r in self._corp_names(text, page):    add(r)
        for r in self._phones(text, page):        add(r)
        for r in self._from_dict(text, page):     add(r)
        return results

    # ── 이메일 ────────────────────────────────────────────────
    def _emails(self, text: str, page: int) -> list:
        out = []
        for m in _P["email"].finditer(text):
            email = m.group()
            if self._is_allowed(email):
                continue
            out.append(DetectionResult(
                page_number=page, detection_type=DetectionType.EMAIL,
                detected_text=email, verdict=VerdictType.VIOLATION,
                reason="이메일 주소 직접 노출 – 제안사 연락처로 식별 가능",
                recommendation="이메일 삭제 또는 'xxx@xxx.xxx' 형태로 마스킹",
                confidence=0.97, source="rule"))
        return out

    # ── URL/도메인 ────────────────────────────────────────────
    # 자동 검출 없음: URL/도메인 모두 사전 등록된 것만 검출 (_from_dict에서 처리)
    def _urls(self, text: str, page: int) -> list:
        return []

    # ── 사업자번호 ────────────────────────────────────────────
    def _biz_numbers(self, text: str, page: int) -> list:
        out = []
        for m in _P["biz_no"].finditer(text):
            out.append(DetectionResult(
                page_number=page, detection_type=DetectionType.BUSINESS_NUMBER,
                detected_text=m.group(), verdict=VerdictType.VIOLATION,
                reason="사업자등록번호 노출 – 법인 직접 식별 가능",
                recommendation="사업자번호 삭제 또는 'XXX-XX-XXXXX' 마스킹",
                confidence=0.99, source="rule"))
        return out

    # ── 법인명 패턴 ───────────────────────────────────────────
    def _corp_names(self, text: str, page: int) -> list:
        out, seen = [], set()
        for m in _P["corp"].finditer(text):
            v = m.group().strip()
            if v in seen or len(v) < 3: continue
            seen.add(v)
            if self._is_allowed(v):
                out.append(DetectionResult(
                    page_number=page, detection_type=DetectionType.COMPANY_NAME,
                    detected_text=v, verdict=VerdictType.ALLOWED,
                    reason="허용 목록 등록 기관명 (발주기관 등)",
                    recommendation="수정 불필요",
                    confidence=0.85, source="rule"))
            else:
                out.append(DetectionResult(
                    page_number=page, detection_type=DetectionType.COMPANY_NAME,
                    detected_text=v, verdict=VerdictType.VIOLATION,
                    reason="법인명 패턴 감지 – 제안사 업체명으로 추정",
                    recommendation="업체명 삭제 또는 '제안사'로 대체",
                    confidence=0.87, source="rule"))
        return out

    # ── 전화번호 ──────────────────────────────────────────────
    def _phones(self, text: str, page: int) -> list:
        out = []
        for m in _P["phone"].finditer(text):
            out.append(DetectionResult(
                page_number=page, detection_type=DetectionType.INDIRECT,
                detected_text=m.group(), verdict=VerdictType.CAUTION,
                reason="전화번호 노출 – 회사 대표번호인 경우 업체 식별 가능",
                recommendation="전화번호 삭제 또는 마스킹",
                confidence=0.72, source="rule"))
        return out

    # ── 사전 기반 ─────────────────────────────────────────────
    def _from_dict(self, text: str, page: int) -> list:
        out, seen = [], set()
        d = self.dictionary

        direct_map = {
            "company_names":        (DetectionType.COMPANY_NAME,   VerdictType.VIOLATION,
                                     "사전 등록 회사명 직접 노출",
                                     "업체명 삭제 또는 '제안사' 대체"),
            "english_names":        (DetectionType.COMPANY_NAME,   VerdictType.VIOLATION,
                                     "사전 등록 영문사명 노출",
                                     "영문사명 삭제 또는 마스킹"),
            "abbreviations":        (DetectionType.COMPANY_NAME,   VerdictType.VIOLATION,
                                     "사전 등록 회사 약칭 노출",
                                     "약칭 삭제 또는 대체"),
            "representative_names": (DetectionType.REPRESENTATIVE, VerdictType.VIOLATION,
                                     "대표자명 직접 노출",
                                     "대표자명 삭제"),
            "personnel_names":      (DetectionType.PERSONNEL,      VerdictType.VIOLATION,
                                     "참여인력 실명 직접 노출",
                                     "성명 삭제, 직책만 표기"),
            "emails":               (DetectionType.EMAIL,          VerdictType.VIOLATION,
                                     "사전 등록 이메일 노출",
                                     "이메일 삭제"),
            "urls":                 (DetectionType.URL,            VerdictType.VIOLATION,
                                     "사전 등록 URL 노출",
                                     "URL 삭제"),
            "domains":              (DetectionType.URL,            VerdictType.VIOLATION,
                                     "사전 등록 도메인 노출",
                                     "도메인 삭제"),
            "brand_names":          (DetectionType.BRAND,          VerdictType.VIOLATION,
                                     "브랜드명 직접 노출",
                                     "브랜드명 삭제 또는 일반명 대체"),
        }
        indirect_map = {
            "color_names":   (DetectionType.COLOR,    "회사 고유 색상명 – 특정 업체 유추 가능",
                               "일반 색상 표기 (RGB값 등)로 변경"),
            "solution_names":(DetectionType.INDIRECT, "특정 업체 전용 솔루션명 – 업체 식별 가능",
                               "일반 솔루션 분류명으로 대체"),
            "slogans":       (DetectionType.SLOGAN,   "회사 고유 슬로건 – 업체 유추 가능",
                               "슬로건 삭제 또는 일반 문구 대체"),
            "org_names":     (DetectionType.INDIRECT, "특정 업체 내부 조직명",
                               "일반 조직명으로 대체"),
            "service_names": (DetectionType.INDIRECT, "특정 업체 서비스명",
                               "서비스 유형명으로 대체"),
        }

        for subcat, (dtype, verdict, reason, rec) in direct_map.items():
            for term in d.get("direct_identifiers", {}).get(subcat, []):
                if not term or len(term) < 2: continue
                # 인력명·대표자명 2글자는 한글 단어 경계 매칭 (false positive 방지)
                # 앞뒤에 한글이 붙어있으면 매칭 제외 (예: '국민은행'에서 '국민' 미탐지)
                if subcat in ("personnel_names", "representative_names") and len(term) <= 2:
                    pattern = r"(?<![가-힣])" + re.escape(term) + r"(?![가-힣])"
                else:
                    pattern = re.escape(term)
                for m in re.finditer(pattern, text, re.I):
                    matched = m.group()
                    k = (matched[:60], dtype)
                    if k in seen: continue
                    seen.add(k)
                    out.append(DetectionResult(
                        page_number=page, detection_type=dtype,
                        detected_text=matched, verdict=verdict,
                        reason=reason, recommendation=rec,
                        confidence=0.99, source="rule"))

        for subcat, (dtype, reason, rec) in indirect_map.items():
            for term in d.get("indirect_identifiers", {}).get(subcat, []):
                if not term or len(term) < 2: continue
                for m in re.finditer(re.escape(term), text, re.I):
                    matched = m.group()
                    k = (matched[:60], dtype)
                    if k in seen: continue
                    seen.add(k)
                    out.append(DetectionResult(
                        page_number=page, detection_type=dtype,
                        detected_text=matched, verdict=VerdictType.CAUTION,
                        reason=reason, recommendation=rec,
                        confidence=0.92, source="rule"))
        return out

    # ── 허용 확인 ─────────────────────────────────────────────
    def _is_allowed(self, term: str) -> bool:
        tl = term.lower()
        for terms in self.dictionary.get("allowed_terms", {}).values():
            for t in terms:
                if t and t.lower() in tl:
                    return True
        return False


_inst: RuleDetector | None = None
def get_rule_detector() -> RuleDetector:
    global _inst
    if _inst is None: _inst = RuleDetector()
    return _inst
