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


def _normalize_name(text: str) -> str:
    """이름 우회 탐지용 정규화: 공백·특수문자·구분기호 제거
    예) '홍 길 동' → '홍길동', '홍·길·동' → '홍길동', '홍_길동' → '홍길동'
    """
    return re.sub(r'[\s\u3000·•_\-/·\.·,]', '', text)


# ── 인명으로 등록될 수 없는 일반 명사/단어 목록 ──────────────────
# 사전에 인력명/대표자명으로 등록돼 있어도 이 목록에 해당하면 허용(맥락상 일반어)으로 처리
_COMMON_WORDS: set[str] = {
    # 일반 명사
    "국민", "전일", "전국", "공공", "민간", "민원", "민간", "대국민",
    "중앙", "지방", "광역", "기초", "지역", "현장", "실무", "담당",
    "대표", "책임", "총괄", "수석", "선임", "주임", "팀장", "부장",
    "이사", "전무", "상무", "전체", "일반", "공통", "기본", "기준",
    "관리", "운영", "개발", "설계", "구축", "분석", "검토", "지원",
    "사업", "과제", "업무", "서비스", "시스템", "솔루션", "플랫폼",
    "전문", "기술", "품질", "보안", "안전", "효율", "성과", "목표",
    "계획", "결과", "현황", "내용", "방법", "방안", "절차", "기준",
    # 사회/공공 관련 일반어 (2글자 인명 오탐 방지)
    "서민", "시민", "주민", "국가", "정부", "공단", "공사", "기관",
    "직원", "구성", "현재", "향후", "기존", "신규", "추진", "수행",
    # 숫자/단위처럼 쓰이는 것
    "일일", "매일", "당일", "익일", "전일", "금일",
}


# ── 기관/단체명 맥락 키워드 (인명 오탐 방지용) ─────────────────────
# 2글자 인력명이 이 단어들과 함께 붙어 있으면 기관명의 일부로 판단해 오탐 처리
_ORG_CONTEXT_SUFFIXES = re.compile(
    r'^(공단|공사|위원회|연구원|연구소|진흥원|협회|학회|재단|센터|기관|'
    r'안전처|안전청|안전부|환경부|복지부|교육부|국방부|외교부|법무부|문화부|과기부|'
    r'청|처|교|대학|병원|학교|'
    r'서비스|시스템|플랫폼|포털|네트워크|솔루션|기업|회사|그룹|주식|유한|'
    r'건강|보험|연금|은행|증권|투자|신용|카드|'
    r'철도|도로|토지|주택|수자원|전력|가스|통신|방송|금융)'
)
_ORG_CONTEXT_PREFIXES = re.compile(
    r'(국가|정부|공공|민간|한국|대한|서울|경기|부산|인천|광주|대전|울산|세종|'
    r'중앙|지방|광역|기초|행정|사업|관리|운영|대국민|시민|주민|대)$'
)


def _is_org_context(term: str, text: str, match_start: int, match_end: int) -> bool:
    """매칭된 위치의 앞뒤 맥락을 보고 기관명의 일부인지 판단"""
    window = 15
    before = text[max(0, match_start - window): match_start]
    after  = text[match_end: match_end + window]

    # 뒤에 기관 관련 단어가 바로 이어지면 기관명 복합어
    if _ORG_CONTEXT_SUFFIXES.search(after):
        return True
    # 앞에 기관 관련 단어가 바로 이어지면 기관명 복합어
    if _ORG_CONTEXT_PREFIXES.search(before):
        return True
    # 앞뒤 모두 한글로 끊김없이 이어지면 복합어의 일부
    if (before and after
            and re.search(r'[가-힣]$', before)
            and re.search(r'^[가-힣]', after)):
        return True
    # 뒤에 한글이 이어지면 복합어 가능성 높음 (2글자 단어 한정)
    if len(term) <= 2 and after and re.search(r'^[가-힣]', after):
        return True
    return False

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
            allowed_reason = self._get_allowed_reason(email)
            if allowed_reason:
                out.append(DetectionResult(
                    page_number=page, detection_type=DetectionType.EMAIL,
                    detected_text=email, verdict=VerdictType.ALLOWED,
                    reason=f"허용 목록 등록 항목 – {allowed_reason}",
                    recommendation="허용 목록에 의해 제외 처리됨",
                    confidence=0.97, source="rule"))
                continue
            out.append(DetectionResult(
                page_number=page, detection_type=DetectionType.EMAIL,
                detected_text=email, verdict=VerdictType.VIOLATION,
                reason="이메일 주소 직접 노출 – 제안사 연락처로 식별 가능",
                recommendation="이메일 삭제 또는 'xxx@xxx.xxx' 형태로 마스킹",
                confidence=0.97, source="rule"))
        return out

    # ── URL/도메인 ────────────────────────────────────────────

    def _urls(self, text: str, page: int) -> list:
        """http/https URL 자동 탐지 — 모든 URL은 주의"""
        out = []
        for m in _P["url"].finditer(text):
            url = m.group().strip()
            if len(url) < 8:
                continue
            allowed_reason = self._get_allowed_reason(url)
            if allowed_reason:
                out.append(DetectionResult(
                    page_number=page, detection_type=DetectionType.URL,
                    detected_text=url, verdict=VerdictType.ALLOWED,
                    reason=f"허용 목록 등록 항목 – {allowed_reason}",
                    recommendation="허용 목록에 의해 제외 처리됨",
                    confidence=0.85, source="rule"))
                continue
            out.append(DetectionResult(
                page_number=page, detection_type=DetectionType.URL,
                detected_text=url, verdict=VerdictType.CAUTION,
                reason="URL 노출 – 제안사 또는 참여인력 유추 가능",
                recommendation="URL 삭제 또는 마스킹 검토",
                confidence=0.85, source="rule"))
        return out

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
        """
        ※ 법인명 패턴 감지 완전 비활성화.
          - 사전에 등록된 업체명은 _from_dict()에서 정확하게 검출
          - 패턴 기반 오탐(컨소시엄사·협력사·발주기관 등) 방지
          - 허용 목록의 ALLOWED 표시도 노이즈이므로 제거
        """
        return []  # 완전 비활성화: 모든 회사명 검출은 사전(_from_dict)에서만 수행

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

        # 이름 정규화 텍스트: 공백·특수문자 제거한 버전도 병행 탐지
        _norm_text = _normalize_name(text)

        for subcat, (dtype, verdict, reason, rec) in direct_map.items():
            for term in d.get("direct_identifiers", {}).get(subcat, []):
                if not term or len(term) < 2: continue

                # 인력명·대표자명: 정규화 텍스트에서도 추가 매칭 (홍 길 동 → 홍길동)
                if subcat in ("personnel_names", "representative_names"):
                    norm_term = _normalize_name(term)

                    # ── 일반 명사 사전 체크: 사람 이름이 될 수 없는 단어 → 허용으로 처리
                    if norm_term in _COMMON_WORDS or term in _COMMON_WORDS:
                        k = (term[:60] + "_common_word", dtype)
                        if k not in seen:
                            seen.add(k)
                            out.append(DetectionResult(
                                page_number=page, detection_type=dtype,
                                detected_text=term,
                                verdict=VerdictType.ALLOWED,
                                reason=f"맥락상 일반 명사로 판단 – 인명 오탐 제외 ('{term}'은 고유 인명이 아님)",
                                recommendation="검증 불필요 – 일반 명사/단어로 확인됨",
                                confidence=0.98, source="rule"))
                        continue

                    # ① 정규화 텍스트에서 매칭 (공백·기호 우회 탐지)
                    if norm_term and len(norm_term) >= 2:
                        norm_pattern = (
                            r"(?<![가-힣])" + re.escape(norm_term) + r"(?![가-힣])"
                            if len(norm_term) <= 2
                            else re.escape(norm_term)
                        )
                        for m in re.finditer(norm_pattern, _norm_text, re.I):
                            # 2글자 이하: 기관명 맥락 오탐 필터 → 허용으로 반환
                            if len(norm_term) <= 2 and _is_org_context(norm_term, _norm_text, m.start(), m.end()):
                                k = (norm_term[:60] + "_allowed_norm", dtype)
                                if k not in seen:
                                    seen.add(k)
                                    out.append(DetectionResult(
                                        page_number=page, detection_type=dtype,
                                        detected_text=term,
                                        verdict=VerdictType.ALLOWED,
                                        reason="맥락상 기관명의 일부로 판단 – 오탐 제외 (공백/기호 분리 형태)",
                                        recommendation="기관명 복합어로 확인됨, 검증 불필요",
                                        confidence=0.97, source="rule"))
                                continue
                            k = (norm_term[:60], dtype)
                            if k in seen: continue
                            seen.add(k)
                            out.append(DetectionResult(
                                page_number=page, detection_type=dtype,
                                detected_text=term,   # 원본 이름 표시
                                verdict=verdict,
                                reason=reason + " (공백/기호 분리 형태 탐지)",
                                recommendation=rec,
                                confidence=0.97, source="rule"))

                    # ② 원문 텍스트에서도 exact 매칭 (앞뒤 한글 경계)
                    if len(term) <= 2:
                        pattern = r"(?<![가-힣])" + re.escape(term) + r"(?![가-힣])"
                    else:
                        pattern = re.escape(term)
                    for m in re.finditer(pattern, text, re.I):
                        # 2글자 이하: 기관명 맥락 오탐 필터 → 허용으로 반환
                        if len(term) <= 2 and _is_org_context(term, text, m.start(), m.end()):
                            matched = m.group()
                            k = (matched[:60] + "_allowed", dtype)
                            if k not in seen:
                                seen.add(k)
                                out.append(DetectionResult(
                                    page_number=page, detection_type=dtype,
                                    detected_text=matched,
                                    verdict=VerdictType.ALLOWED,
                                    reason="맥락상 기관명의 일부로 판단 – 오탐 제외",
                                    recommendation="기관명 복합어로 확인됨, 검증 불필요",
                                    confidence=0.99, source="rule"))
                            continue
                        matched = m.group()
                        k = (matched[:60], dtype)
                        if k in seen: continue
                        seen.add(k)
                        out.append(DetectionResult(
                            page_number=page, detection_type=dtype,
                            detected_text=matched, verdict=verdict,
                            reason=reason, recommendation=rec,
                            confidence=0.99, source="rule"))
                else:
                    # 인력명 외 항목 (company_names, brand_names 등)
                    # 한국어 단어 경계: 앞뒤가 한글로 이어지지 않아야 함 (복합어 오탐 방지)
                    # 단, 3글자 이상 업체명은 경계 없이 포함 탐지
                    if len(term) <= 4:
                        # 짧은 단어는 앞뒤 한글 경계 체크
                        pattern = r"(?<![가-힣A-Za-z])" + re.escape(term) + r"(?![가-힣A-Za-z])"
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
        return bool(self._get_allowed_reason(term))

    def _get_allowed_reason(self, term: str) -> str:
        """허용 목록에 해당하면 이유 문자열 반환, 아니면 빈 문자열 반환"""
        tl = term.lower()
        for cat, terms in self.dictionary.get("allowed_terms", {}).items():
            for t in terms:
                if t and t.lower() in tl:
                    return f"{cat} 허용 목록 ({t})"
        return ""


_inst: RuleDetector | None = None
def get_rule_detector() -> RuleDetector:
    global _inst
    if _inst is None: _inst = RuleDetector()
    return _inst
