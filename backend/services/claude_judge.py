"""
Claude API 판정 엔진
– claude-3-5-sonnet 으로 블라인드 최종 심사
– 페이지 단위 배치 판정 (비용 절감)
– API 키 없으면 규칙 기반 폴백
"""
from __future__ import annotations
import json, re
from typing import List, Optional
from models.schemas import DetectionResult, DetectionType, VerdictType
import core.config as _cfg
from core.config import get_logger

logger = get_logger("claude_judge")

# ── 시스템 프롬프트 ────────────────────────────────────────────
SYSTEM = """너는 공공입찰 제안서 블라인드 검수 심사관이다.

문서에서 입찰자(제안사)를 식별할 수 있는 정보가 있는지 판단하라.

## 판정 기준

### 위반 (직접 식별)
- 업체명, 대표자명, 참여인력 실명
- 제안사 로고·CI·BI
- 회사 이메일, 도메인, 홈페이지 URL
- 회사명이 보이는 캡처 화면
- PDF 메타데이터의 제안사 식별정보
- 회사명 워터마크, 사업자번호

### 주의 (간접 식별)
- 회사 고유 색상명
- 특정 업체만 쓰는 내부 솔루션명
- 특정 실적 조합으로 업체를 강하게 유추할 수 있는 경우
- 브랜드·마크를 추정할 수 있는 이미지 설명
- 특정 회사 특유의 슬로건·표현

### 허용
- 발주기관명·로고
- 대상사업명, 공고문 공식 사업명
- 일반적인 기술·방법론 설명
- 수행사명 없는 유사 실적 설명
- 일반적인 품질보증·감리 설명

## 핵심 원칙
발주기관 정보 → 허용 / 제안사 식별정보 → 금지
불확실한 경우 → 보수적으로 '주의' 판정

## 응답 형식 (JSON 배열, 다른 내용 금지)
[
  {
    "idx": 0,
    "verdict": "위반"|"주의"|"허용",
    "detection_type": "업체명"|"대표자명"|"참여인력명"|"로고/CI/BI"|"이메일"|"URL/도메인"|"브랜드명"|"회사 고유 색상"|"메타데이터"|"이미지 내 텍스트"|"간접 식별 표현"|"워터마크"|"사업자번호"|"슬로건/고유표현"|"기타",
    "reason": "판정 사유 (1~3문장)",
    "recommendation": "수정 권고안 (구체적인 대체 문구 포함)",
    "confidence": 0.0~1.0
  }
]"""


# ── Claude 클라이언트 ─────────────────────────────────────────
class ClaudeJudge:
    def __init__(self):
        self.enabled = _cfg.CLAUDE_ENABLED
        self._client = None
        if self.enabled:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=_cfg.ANTHROPIC_API_KEY)
                logger.info(f"Claude 준비: {_cfg.CLAUDE_MODEL}")
            except Exception as e:
                logger.warning(f"Claude 초기화 실패: {e}")
                self.enabled = False
        else:
            logger.info("ANTHROPIC_API_KEY 없음 → 규칙 기반만 사용")

    # ── 메타데이터 판정 ───────────────────────────────────────
    def judge_metadata(self, metadata: dict, allowed_check_fn) -> List[DetectionResult]:
        results = []
        sensitive = {
            "author":   "문서 작성자",
            "creator":  "문서 생성 프로그램/회사",
            "producer": "PDF 생성 프로그램/회사",
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
                    recommendation=f"파일 저장 전 '{field}' 메타데이터 삭제 (Adobe Acrobat → 파일 속성 → 설명 탭 초기화)",
                    confidence=0.85, source="rule"))
        return results

    # ── 페이지 배치 판정 ──────────────────────────────────────
    def judge_page_batch(self, items: List[dict]) -> List[dict]:
        """
        items: [{"idx":int, "text":str, "page":int, "type":str, "source":str}, ...]
        반환:  [{"idx":int, "verdict":str, "detection_type":str,
                 "reason":str, "recommendation":str, "confidence":float}, ...]
        """
        if not items:
            return []
        if not self.enabled:
            return self._fallback(items)

        # 입력 텍스트 구성
        lines = []
        for it in items:
            lines.append(
                f'[{it["idx"]}] 페이지:{it["page"]} | 유형:{it["type"]} | '
                f'내용:"{it["text"][:200]}"'
            )

        user_msg = (
            "다음 항목들을 블라인드 심사하라.\n\n"
            + "\n".join(lines)
            + "\n\n반드시 JSON 배열만 반환하라."
        )

        try:
            resp = self._client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
            return self._parse_response(raw, items)
        except Exception as e:
            logger.error(f"Claude API 오류: {e}")
            return self._fallback(items)

    # ── 전체 페이지 텍스트 컨텍스트 판정 ─────────────────────
    def judge_full_context(self, page_text: str, page_num: int,
                           ocr_text: str = "", metadata_str: str = "") -> List[dict]:
        """페이지 전체 텍스트를 Claude에게 던져 추가 위반 탐지"""
        if not self.enabled or not page_text.strip():
            return []

        combined = page_text
        if ocr_text.strip():
            combined += f"\n\n[OCR 추출]\n{ocr_text}"
        if metadata_str:
            combined += f"\n\n[메타데이터]\n{metadata_str}"

        user_msg = (
            f"다음은 공공입찰 제안서 {page_num}페이지의 전체 텍스트다.\n"
            "블라인드 위반 요소를 모두 찾아 JSON 배열로 반환하라.\n"
            "없으면 빈 배열 [] 반환.\n\n"
            f"=== 텍스트 ===\n{combined[:3000]}\n"
            "=== 끝 ===\n\n반드시 JSON 배열만 반환하라."
        )

        try:
            resp = self._client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=2048,
                system=SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = resp.content[0].text.strip()
            parsed = self._extract_json_array(raw)
            # idx 없으면 부여
            for i, item in enumerate(parsed):
                item.setdefault("idx", i)
                item.setdefault("page", page_num)
            return parsed
        except Exception as e:
            logger.warning(f"컨텍스트 판정 오류 p{page_num}: {e}")
            return []

    # ── 응답 파싱 ─────────────────────────────────────────────
    def _parse_response(self, raw: str, items: List[dict]) -> List[dict]:
        parsed = self._extract_json_array(raw)
        if not parsed:
            return self._fallback(items)
        # idx 누락 대비
        for i, r in enumerate(parsed):
            r.setdefault("idx", items[i]["idx"] if i < len(items) else i)
        return parsed

    def _extract_json_array(self, raw: str) -> list:
        """응답에서 JSON 배열 추출 (마크다운 코드블록 등 처리)"""
        # ```json ... ``` 제거
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        # 첫 번째 [ 부터 마지막 ] 까지 추출
        s = raw.find("[")
        e = raw.rfind("]")
        if s == -1 or e == -1:
            return []
        try:
            return json.loads(raw[s : e + 1])
        except json.JSONDecodeError:
            logger.warning(f"JSON 파싱 실패: {raw[:200]}")
            return []

    # ── 폴백 ─────────────────────────────────────────────────
    def _fallback(self, items: List[dict]) -> List[dict]:
        """Claude 없을 때 규칙 결과 그대로 반환"""
        out = []
        verdict_map = {
            "업체명": "위반", "대표자명": "위반", "참여인력명": "위반",
            "이메일": "위반", "URL/도메인": "위반", "로고/CI/BI": "위반",
            "사업자번호": "위반", "메타데이터": "위반",
            "회사 고유 색상": "주의", "간접 식별 표현": "주의",
            "브랜드명": "위반", "워터마크": "위반", "슬로건/고유표현": "주의",
        }
        for it in items:
            out.append({
                "idx":            it["idx"],
                "verdict":        verdict_map.get(it.get("type", ""), "주의"),
                "detection_type": it.get("type", "기타"),
                "reason":         f"규칙 기반 탐지: {it.get('type','기타')} 유형 감지",
                "recommendation": "해당 항목 검토 후 필요 시 수정",
                "confidence":     0.70,
            })
        return out


# 싱글톤
_inst: ClaudeJudge | None = None
def get_claude_judge() -> ClaudeJudge:
    global _inst
    if _inst is None: _inst = ClaudeJudge()
    return _inst

def _reset_judge():
    """런타임 API 키 변경 시 Claude 클라이언트 재초기화"""
    global _inst
    _inst = None
    return get_claude_judge()
