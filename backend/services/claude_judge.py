"""
Claude Vision 판정 엔진 v3
- PAGE 블록 구조로 페이지 간 전이 완전 차단
- rule_hits 확정→후보 교차검증 방식
- 전체 이름 일치 기준 적용
- 이름+직책 문맥 조건 강화
- 레퍼런스 없는 로고 위반→주의 처리
- _parse_items() 출력 검증 강화
"""
from __future__ import annotations
import json, re, base64
from typing import List, Optional
import core.config as _cfg
from core.config import get_logger

logger = get_logger("claude_judge")

# ── 시스템 프롬프트 ─────────────────────────────────────────────
SYSTEM_PROMPT = """너는 공공입찰 제안서 블라인드 검증 전문 심사관이다.
목표는 제안사(입찰자) 또는 참여인력을 식별할 수 있는 정보를 찾아내는 것이다.

━━━ 핵심 판정 원칙 ━━━
입력은 [PAGE N START] ... [PAGE N END] 블록으로 구분된다.
각 블록은 완전히 독립적으로 판단하라.
★★★ 다른 PAGE 블록에서 발견한 내용을 현재 블록에 적용하는 것 절대 금지 ★★★
★★★ 어떤 페이지에 위반이 있어도 다른 페이지에 자동 확장 금지 ★★★

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

━━━ 우선순위 4-B — 회사 로고 ━━━
【로고 레퍼런스가 있는 경우】
- 레퍼런스와 형태·색상·폰트 스타일이 모두 80% 이상 일치해야만 【위반】
- 비슷해 보이는 도형·아이콘·배지·UI 요소는 위반 아님
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
      "recommendation": "수정 권고 (허용이면 빈 문자열)"
    }
  ]
}
- 문제 없는 페이지는 포함하지 않아도 된다
- 한 페이지에 위반 요소 N개면 items 배열에 N개 항목"""


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
                "text": "아래는 제안사 공식 로고 레퍼런스 이미지이다.\n판정 기준: 형태·색상·폰트가 모두 80% 이상 일치해야만 위반.\n단순히 비슷해 보이는 도형·아이콘·배지·UI 요소는 위반 아님.\n발주기관(공공기관) 로고는 절대 위반 아님."
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
                "text": f"[PAGE {page_num} END]\n이 페이지는 위 이미지만을 근거로 독립적으로 판정하라. 다른 PAGE 블록 내용 참조 금지."
            })

        # 최종 지시
        page_list = ", ".join(str(p) for p in valid_pages)
        content.append({
            "type": "text",
            "text": (
                f"위 PAGE 블록들({page_list})을 블라인드 검증하라.\n"
                f"각 PAGE 블록은 독립적으로 판단하라. 블록 간 내용 전이 금지.\n"
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
            return self._parse_items(raw, valid_pages)
        except Exception as e:
            logger.error(f"Claude Vision 오류 p{valid_pages}: {e}")
            return []

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
