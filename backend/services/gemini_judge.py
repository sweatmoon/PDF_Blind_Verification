"""
Gemini Vision 판정 엔진
- gemini-2.5-flash 모델 사용 (저비용 고성능)
- 페이지 1장씩 독립 분석 (배치 오탐 원천 차단)
- Claude SYSTEM_PROMPT와 동일한 판정 기준 적용
- 이미지 토큰: ~258/장 (Claude 1,328 대비 1/5 수준)
"""
from __future__ import annotations
import json, re, base64
from typing import List, Optional
import core.config as _cfg
from core.config import get_logger

logger = get_logger("gemini_judge")

GEMINI_MODEL = "gemini-2.5-flash-preview-04-17"

# Claude와 동일한 시스템 프롬프트 재사용
from services.claude_judge import SYSTEM_PROMPT


class GeminiVisionJudge:

    def __init__(self):
        self._client = None
        self.enabled = False
        self._init()

    def _init(self):
        key = _cfg.get_gemini_key()
        if not key:
            logger.info("Gemini API 키 미설정 — GeminiVisionJudge 비활성")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=key)
            self.enabled = True
            logger.info(f"GeminiVisionJudge 초기화 완료 ({GEMINI_MODEL})")
        except Exception as e:
            logger.error(f"GeminiVisionJudge 초기화 실패: {e}")

    def reload(self):
        """API 키 변경 후 재초기화"""
        self._client = None
        self.enabled = False
        self._init()

    # ── 페이지 1장 단독 판정 (핵심) ──────────────────────────────
    def judge_single_page(
        self,
        page_image: dict,          # {"page": int, "b64": str, "media_type": str}
        logo_b64: Optional[str],
        company_dict: Optional[dict] = None,
        rule_hits: Optional[list] = None,  # 이 페이지의 rule_hits만
    ) -> List[dict]:
        """
        슬라이드 1장만 독립 분석 → 배치 오탐 원천 차단
        rule_hits: 이 페이지의 텍스트 탐지 결과 (다른 페이지 것 절대 전달 X)
        """
        if not self.enabled or not page_image:
            return []

        from google.genai import types

        parts = []

        # 1. 시스템 지시 (인라인 — Gemini는 system_instruction 별도 지원)
        # → messages의 첫 번째 user 파트에 포함

        # 2. 로고 레퍼런스
        if logo_b64:
            parts.append(types.Part.from_text(
                "아래는 제안사 공식 로고 레퍼런스 이미지이다.\n"
                "이 로고와 형태·색상·폰트가 모두 명확히 일치하는 경우에만 위반으로 판정하라.\n"
                "단순히 비슷한 도형·아이콘은 위반 아님. 확신도 80% 미만이면 허용으로 판정하라.\n"
                "발주기관(공공기관) 로고는 이 레퍼런스와 무관하게 절대 위반 아님."
            ))
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(logo_b64),
                mime_type="image/png"
            ))

        # 3. 사전 정보
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
            rep = _get("representative_names")
            if rep:
                lines.append(f"대표자: {', '.join(rep)}")
            emails = _get("emails")
            if emails:
                lines.append(f"이메일: {', '.join(emails)}")
            domains = _get("domains")
            if domains:
                lines.append(f"도메인: {', '.join(domains)}")

            indirect = company_dict.get("indirect_identifiers", {})
            for k, label in [("color_names","고유색상"),("solution_names","솔루션명")]:
                vals = [str(x).strip() for x in (indirect.get(k) or []) if str(x).strip()]
                if vals:
                    lines.append(f"{label}: {', '.join(vals)}")

            if lines:
                parts.append(types.Part.from_text(
                    "━━━ 제안사 식별 사전 (이 정보가 등장하면 즉시 위반) ━━━\n"
                    + "\n".join(lines)
                ))

        # 4. rule_hits 힌트 (이 페이지 것만)
        page_num = page_image["page"]
        if rule_hits:
            violations = [h for h in rule_hits if h.get("judgment") == "위반"]
            cautions   = [h for h in rule_hits if h.get("judgment") == "주의"]
            hint_lines = [
                f"[{page_num}페이지 텍스트 레이어에서 이미 탐지된 항목 — "
                f"아래 항목을 page: \"{page_num}\" 로 JSON에 포함할 것]"
            ]
            for h in violations:
                hint_lines.append(
                    f'  - page: "{page_num}", type: "{h.get("type","")}", '
                    f'content: "{h.get("content","")}", judgment: "위반"'
                )
            for h in cautions:
                hint_lines.append(
                    f'  - page: "{page_num}", type: "{h.get("type","")}", '
                    f'content: "{h.get("content","")}", judgment: "주의"'
                )
            hint_lines.append(
                f"※ content 값을 그대로 JSON에 출력하십시오. "
                f"요약하거나 변경하지 마십시오."
            )
            parts.append(types.Part.from_text("\n".join(hint_lines)))

        # 5. 페이지 이미지
        media_type = page_image.get("media_type", "image/jpeg")
        parts.append(types.Part.from_bytes(
            data=base64.b64decode(page_image["b64"]),
            mime_type=media_type
        ))
        parts.append(types.Part.from_text(
            f"위 이미지는 제안서 {page_num}페이지입니다."
        ))

        # 6. 출력 지시
        parts.append(types.Part.from_text(
            f"{page_num}페이지를 블라인드 검증하고 JSON만 반환하라. "
            "텍스트 탐지에서 위반이 확인된 항목은 반드시 포함하라."
        ))

        try:
            from google.genai import types as gtypes
            response = self._client.models.generate_content(
                model=GEMINI_MODEL,
                contents=parts,
                config=gtypes.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.0,
                    max_output_tokens=2048,
                ),
            )
            raw = response.text.strip()
            logger.info(f"Gemini 응답 p{page_num}: {len(raw)}자")
            return self._parse_items(raw)
        except Exception as e:
            logger.error(f"Gemini Vision 오류 p{page_num}: {e}")
            return []

    # ── 배치 인터페이스 (ppt_pipeline 호환용) ────────────────────
    def judge_image_batch(
        self,
        page_images: List[dict],
        logo_b64: Optional[str],
        company_dict: Optional[dict] = None,
        rule_hits: Optional[dict] = None,
    ) -> List[dict]:
        """
        ppt_pipeline과 동일한 인터페이스.
        내부적으로 1장씩 순차 처리 → 배치 오탐 원천 차단.
        """
        results = []
        for pg in page_images:
            page_key = str(pg["page"])
            page_rule_hits = (rule_hits or {}).get(page_key, [])
            items = self.judge_single_page(pg, logo_b64, company_dict, page_rule_hits)
            results.extend(items)
        return results

    # ── 응답 파싱 ────────────────────────────────────────────────
    def _parse_items(self, raw: str) -> List[dict]:
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        s = raw.find("{")
        e = raw.rfind("}")
        if s == -1 or e == -1:
            logger.warning(f"JSON 없음: {raw[:200]}")
            return []
        try:
            obj = json.loads(raw[s:e+1])
            items = obj.get("items", [])
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
_inst: GeminiVisionJudge | None = None

def get_gemini_judge() -> GeminiVisionJudge:
    global _inst
    if _inst is None:
        _inst = GeminiVisionJudge()
    return _inst

def reset_gemini_judge():
    global _inst
    _inst = None
