"""
Claude Vision 판정 엔진 v2
- claude-sonnet-4-20250514 (Vision 지원)
- 페이지 이미지 배치 6장 단위 직접 분석
- 로고 레퍼런스 이미지 포함
- JSON items[] 형식 출력
- 규칙 기반 폴백
"""
from __future__ import annotations
import json, re, base64
from typing import List, Optional
import core.config as _cfg
from core.config import get_logger

logger = get_logger("claude_judge")

# ── 시스템 프롬프트 (사용자 제공 스펙) ─────────────────────────
SYSTEM_PROMPT = """너는 공공입찰 제안서 블라인드 검증 전문 심사관이다.
목표는 제안사(입찰자) 또는 참여인력을 식별할 수 있는 정보만 정확히 찾아내는 것이다.

━━━ 인력 정보 탐지 강화 원칙 (최최우선, 다른 모든 규칙보다 우선) ━━━
참여인력 관련 페이지는 반드시 아래 기준으로 판정하라.

【사전 이름 매칭 — 절대 우선】
- 아래 "참여인력 실명 목록"에 있는 이름이 이미지에서 조금이라도 보이면 → 즉시 【위반】
- 사전 이름이 보이면 익명처리 여부, 맥락, 다른 규칙 모두 무시하고 무조건 위반

【사전 이름이 없더라도 위반인 경우】
- 2~4글자 한글 이름 + 직책(기술사·감리사·책임자·매니저·과장·부장 등)이 함께 있으면 → 【위반】
- 2~4글자 한글 이름 + 학력/경력 정보가 함께 있으면 → 【위반】
- 인물 사진·실루엣 옆에 이름처럼 보이는 한글 텍스트 → 【위반】
- 인력 소개 카드·프로필 박스·조직도에서 한글 이름 발견 → 【위반】

【익명 처리 인정 기준 — 매우 엄격하게 적용】
- 이름 자리에 아래 마스킹 패턴이 표기된 경우 익명으로 인정:
  OOO, ○○○, ***, 홍○○, 홍길○, *000, *00, 000, 00O, 홍*동, 홍00 등
  즉, O·○·*·0(숫자) 등 마스킹 기호로 이름 일부 또는 전체가 대체된 형태는 모두 익명으로 인정
- "*000 (AI강리원)", "OOO (데이터 감리원)" 처럼 이름이 마스킹되고 역할/직책만 표기된 형태 → 【허용】
  역할명만으로는 특정 인물을 식별할 수 없으므로 익명 처리 완료로 본다
- 단, 사전에 있는 이름(위 실명 목록)이 같은 페이지 어디에든 한 번이라도 나오면 익명처리와 무관하게 위반
- "모든 이름이 OOO으로 처리됐다"는 전체적 요약 판단 금지 — 각 이름을 개별적으로 확인하라

━━━ 절대 위반 처리 금지 (허용 목록) ━━━
발주기관명/로고, 실적 발주처명, 공공기관명(한국남동발전·한국전력·행정안전부·금감원 등),
사업명, 일반 기술/제품명, JMeter 등 오픈소스, 일반 실루엣/아이콘

━━━ 판정 기준 ━━━
【위반】제안사명/영문명/약칭, 제안사 로고, 제안사 도메인/이메일,
       대표자·참여인력 실명, 조직도 내 실명, 실제 얼굴 사진, 명함, 회사명 노출 캡처, 워터마크
【주의】제안사 내부 솔루션명 추정, 특정 업체 고유 문구 추정, 확정 불가한 브랜드명,
       얼굴 여부 불명확한 이미지
       ※ 이름이 마스킹 기호(OOO/*000/000 등)로 처리된 경우 역할명이 함께 있어도 → 【허용】
【허용】발주기관명/로고, 실적 발주처명, 공공기관명, 사업명, 일반 기술/오픈소스명,
       완전한 익명 처리(OOO/*000/000 등 마스킹, 사전 이름 없는 경우),
       "*000 (역할명)" 형태처럼 이름 마스킹 + 역할만 표기된 인력 정보, 일반 아이콘

━━━ 판정 우선순위 ━━━
1. 사전 실명 목록에 있는 이름이 이미지에 보이면 → 무조건 위반 (최우선)
2. 이름+직책 또는 이름+경력 조합이 보이면 → 위반
3. 허용 목록에 해당하면 → 허용
4. 그 외 애매한 경우만 → 주의

━━━ 반복 항목 처리 ━━━
같은 로고·워터마크·하단 표기가 여러 페이지 반복되면 "p.5~12 하단 로고 반복"처럼 범위로 묶어라.

━━━ 텍스트 추출 사전 탐지 결과 우선 처리 (매우 중요) ━━━
각 페이지 이미지 앞에 "[텍스트 탐지 결과]"가 제공될 수 있다.
- 텍스트 추출로 이미 "위반" 판정된 항목이 있는 페이지는 → 이미지 분석 결과와 무관하게 반드시 위반으로 출력하라
- 텍스트로 감지된 실명(예: 서희명)이 있으면 해당 페이지는 무조건 위반으로 포함시켜라
- 텍스트 탐지 결과가 없는 페이지도 이미지를 꼼꼼히 확인하라

━━━ 절대 금지 사항 ━━━
- 사전 실명 목록에 있는 이름이 이미지에서 보이면 절대 허용이나 생략 불가 — 반드시 위반으로 출력
- "전반적으로 OOO 처리됐다"는 요약 판단 절대 금지 — 페이지를 하나씩 개별 확인하라
- 배치 내 다른 페이지에 OOO이 있다고 해서 이 페이지도 익명처리됐다고 가정하지 말라
- 사전 이름이 보이는 항목을 items에서 누락하거나 허용으로 분류하는 것은 오류다

반드시 아래 JSON 형식으로만 반환하라. 다른 텍스트 절대 포함 금지:
{
  "items": [
    {
      "page": "페이지 또는 범위(예: 5 또는 5~12)",
      "type": "검출 유형",
      "content": "검출 내용 (사전 이름이면 해당 이름 명시)",
      "judgment": "위반 또는 주의 또는 허용",
      "reason": "판정 사유",
      "recommendation": "수정 권고 (허용이면 없음)"
    }
  ]
}
문제 없는 페이지는 포함하지 않아도 된다. 단, 텍스트 탐지에서 위반이 확인된 페이지는 반드시 포함하라."""


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
        """메타데이터는 규칙 기반으로 처리 (이미지 불필요)"""
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
        page_images: List[dict],   # [{"page": int, "b64": str, "media_type": str}, ...]
        logo_b64: Optional[str],   # 로고 레퍼런스 이미지 base64 (PNG)
        company_dict: Optional[dict] = None,  # 회사 사전 정보
        rule_hits: Optional[dict] = None,      # 텍스트 추출 규칙 탐지 결과 { "pageNum": [...] }
    ) -> List[dict]:
        """
        페이지 이미지 배치를 Claude Vision으로 분석
        rule_hits: scan-text 결과, 페이지별 규칙 탐지 항목 → 이미지 분석 힌트로 삽입
        반환: [{"page":"1~3","type":"업체명","content":"...","judgment":"위반","reason":"...","recommendation":"..."}]
        """
        if not page_images:
            return []
        if not self.enabled:
            return []

        content = []

        # 1. 로고 레퍼런스 첨부
        if logo_b64:
            content.append({
                "type": "text",
                "text": "아래는 제안사 공식 로고 레퍼런스 이미지이다. 이 로고 또는 유사 로고가 문서에 등장하면 위반으로 판정하라."
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": logo_b64
                }
            })

        # 2. 회사 사전 정보 텍스트 추가
        if company_dict:
            direct = company_dict.get("direct_identifiers", company_dict)  # 중첩 구조 or flat 구조 모두 지원

            def _get(key):
                # 중첩(direct_identifiers.xxx) 또는 flat(xxx) 두 형태 모두 처리
                v = direct.get(key) or company_dict.get(key) or []
                return [str(x).strip() for x in v if str(x).strip()]

            lines = []

            names = _get("company_names")
            if names:
                lines.append(f"제안사명: {', '.join(names)}")

            eng = _get("english_names")
            if eng:
                lines.append(f"영문명·도메인: {', '.join(eng)}")

            abbr = _get("abbreviations")
            if abbr:
                lines.append(f"약칭: {', '.join(abbr)}")

            rep = _get("representative_names")
            if rep:
                lines.append(f"대표자: {', '.join(rep)}")

            # ★★★ 참여인력 실명 목록 — 최우선 위반 트리거 (별도 블록으로 강조)
            personnel = _get("personnel_names")
            if personnel:
                # 사전 이름은 다른 정보와 섞지 않고 별도 텍스트 블록으로 강조 전달
                lines.append(
                    f"【절대 위반 트리거 — 실명 목록】\n"
                    f"아래 이름 중 하나라도 이미지 어디에서든 보이면 OOO 처리 여부와 무관하게 즉시 위반으로 판정하라.\n"
                    f"이름: {', '.join(personnel)}"
                )

            emails = _get("emails")
            if emails:
                lines.append(f"이메일·도메인: {', '.join(emails)}")

            domains = _get("domains")
            if domains:
                lines.append(f"도메인: {', '.join(domains)}")

            brands = _get("brand_names")
            if brands:
                lines.append(f"브랜드명: {', '.join(brands)}")

            # 간접 식별자
            indirect = company_dict.get("indirect_identifiers", {})
            for k, label in [("color_names","고유색상"), ("solution_names","솔루션명"),
                              ("slogans","슬로건"), ("org_names","조직명"), ("service_names","서비스명")]:
                vals = [str(x).strip() for x in (indirect.get(k) or []) if str(x).strip()]
                if vals:
                    lines.append(f"{label}: {', '.join(vals)}")

            if lines:
                content.append({
                    "type": "text",
                    "text": "━━━ 제안사 식별 사전 (이 정보가 등장하면 즉시 위반) ━━━\n" + "\n".join(lines)
                })

        # 3. 페이지 이미지들 첨부 (각 페이지 앞에 텍스트 탐지 힌트 삽입)
        start_page = page_images[0]["page"]
        end_page   = page_images[-1]["page"]
        for pg in page_images:
            page_key = str(pg["page"])
            # 이 페이지에 대한 규칙 탐지 결과가 있으면 이미지 앞에 힌트 삽입
            if rule_hits and page_key in rule_hits and rule_hits[page_key]:
                hits = rule_hits[page_key]
                violations = [h for h in hits if h.get("judgment") == "위반"]
                cautions   = [h for h in hits if h.get("judgment") == "주의"]
                hint_lines = [f"⚠️ [{pg['page']}페이지 텍스트 추출 탐지 결과 — 반드시 위반으로 판정할 것]"]
                for h in violations:
                    hint_lines.append(f"  【위반 확정】{h.get('type','')}: \"{h.get('content','')}\" → 반드시 위반으로 출력하라")
                for h in cautions:
                    hint_lines.append(f"  【주의 확정】{h.get('type','')}: \"{h.get('content','')}\" → 반드시 주의 이상으로 출력하라")
                hint_lines.append("위 항목들은 텍스트 추출로 이미 확인된 사실이므로 이미지에서 보이지 않더라도 반드시 위반으로 포함시켜라.")
                content.append({
                    "type": "text",
                    "text": "\n".join(hint_lines)
                })

            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": pg.get("media_type", "image/jpeg"),
                    "data": pg["b64"]
                }
            })
            content.append({
                "type": "text",
                "text": f"위 이미지는 제안서 {pg['page']}페이지입니다."
            })

        content.append({
            "type": "text",
            "text": f"페이지 {start_page}~{end_page}을 블라인드 검증하고 JSON만 반환하라. 텍스트 탐지에서 위반이 확인된 페이지는 반드시 포함하라."
        })

        try:
            resp = self._client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
            raw = resp.content[0].text.strip()
            logger.info(f"Claude 응답 p{start_page}~{end_page}: {len(raw)}자")
            return self._parse_items(raw)
        except Exception as e:
            logger.error(f"Claude Vision 오류 p{start_page}~{end_page}: {e}")
            return []

    # ── 응답 파싱 ────────────────────────────────────────────────
    def _parse_items(self, raw: str) -> List[dict]:
        """JSON { items: [...] } 파싱"""
        # 코드블록 제거
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        # { ... } 추출
        s = raw.find("{")
        e = raw.rfind("}")
        if s == -1 or e == -1:
            logger.warning(f"JSON 객체 없음: {raw[:200]}")
            return []
        try:
            obj = json.loads(raw[s:e+1])
            items = obj.get("items", [])
            # 필수 필드 보정
            cleaned = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                it.setdefault("page", "?")
                it.setdefault("type", "기타")
                it.setdefault("content", "")
                it.setdefault("judgment", "주의")
                it.setdefault("reason", "")
                it.setdefault("recommendation", "")
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
