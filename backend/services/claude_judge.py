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
목표는 제안사(입찰자) 또는 참여인력을 식별할 수 있는 정보만 정확히 찾아내는 것이다. 과잉탐지 금지.

━━━ 인력 정보 탐지 강화 원칙 ━━━
참여인력 관련 페이지는 반드시 아래 기준으로 재확인하라.
- 이름처럼 보이는 2~4글자 한글 텍스트가 있으면 반드시 실명 여부를 판단하라.
- OOO·○○○으로 표기된 경우만 익명 처리로 인정한다.
- 실제 한글 이름(예: 홍길동, 서희명, 김종훈 등)이 보이면 무조건 위반이다.
- 이름 옆에 직책(기술사, 감리사, 책임자 등)이 함께 있으면 참여인력 실명일 가능성이 매우 높다.
- 인물 사진 또는 실루엣 옆에 텍스트가 있으면 해당 텍스트가 실명인지 반드시 확인하라.
- 인력 소개 카드, 프로필 박스, 조직도 형태의 레이아웃에서는 텍스트를 더욱 꼼꼼히 읽어라.

━━━ 오탐지 방지 원칙 (최최우선 적용) ━━━
제안사 영문명은 반드시 아래 표현과 정확히 일치할 때만 위반으로 판정하라.
확신이 없으면 반드시 주의 또는 허용으로 분류하라. 오탐지는 미탐지보다 훨씬 나쁘다.

━━━ 절대 위반 처리 금지 ━━━
발주기관명/로고, 실적 발주처명, 공공기관명(한국남동발전·한국전력·행정안전부·금감원 등),
사업명, 일반 기술/제품명, JMeter 등 오픈소스, OOO·○○○·*** 익명 처리 표현, 일반 실루엣/아이콘

━━━ 판정 기준 ━━━
【위반】제안사명/영문명/약칭 표기, 제안사 로고, 제안사 도메인/이메일,
       대표자·참여인력 실명, 조직도 내 실명, 실제 얼굴 사진, 명함, 회사명 노출 캡처, 워터마크
【주의】제안사 내부 솔루션명 추정, 특정 업체 고유 문구 추정, 확정 불가한 브랜드명,
       익명화는 됐으나 맥락상 유추 가능성 있는 경우, 얼굴 여부 불명확한 이미지
【허용】발주기관명/로고, 실적 발주처명, 공공기관명, 사업명, 일반 기술/오픈소스명, 익명 처리, 일반 아이콘

━━━ 반복 항목 처리 ━━━
같은 로고·워터마크·하단 표기가 여러 페이지 반복되면 "p.5~12 하단 로고 반복"처럼 범위로 묶어 하나의 item으로 정리하라.

반드시 아래 JSON 형식으로만 반환하라. 다른 텍스트 절대 포함 금지:
{
  "items": [
    {
      "page": "페이지 또는 범위(예: 5 또는 5~12)",
      "type": "검출 유형",
      "content": "검출 내용",
      "judgment": "위반 또는 주의 또는 허용",
      "reason": "판정 사유",
      "recommendation": "수정 권고 (허용이면 없음)"
    }
  ]
}
문제 없는 페이지는 포함하지 않아도 된다. 허용 항목도 중요한 것만 포함하라."""


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
    ) -> List[dict]:
        """
        페이지 이미지 배치를 Claude Vision으로 분석
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
            dict_lines = []
            if company_dict.get("company_names"):
                dict_lines.append(f"제안사명: {', '.join(company_dict['company_names'])}")
            if company_dict.get("emails"):
                dict_lines.append(f"이메일 도메인: {', '.join(company_dict['emails'])}")
            if company_dict.get("domains"):
                dict_lines.append(f"도메인: {', '.join(company_dict['domains'])}")
            if company_dict.get("representative_names"):
                dict_lines.append(f"대표자: {', '.join(company_dict['representative_names'])}")
            if dict_lines:
                content.append({
                    "type": "text",
                    "text": "━━━ 제안사 식별 사전 (이 정보가 등장하면 즉시 위반) ━━━\n" + "\n".join(dict_lines)
                })

        # 3. 페이지 이미지들 첨부
        start_page = page_images[0]["page"]
        end_page   = page_images[-1]["page"]
        for pg in page_images:
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
            "text": f"페이지 {start_page}~{end_page}을 블라인드 검증하고 JSON만 반환하라."
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
