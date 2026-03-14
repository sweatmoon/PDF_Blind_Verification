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
목표는 제안사(입찰자) 또는 참여인력을 식별할 수 있는 정보를 빠짐없이 찾아내는 것이다.

━━━ 판정 최우선 원칙: 익명성 위배 탐지 ━━━
블라인드 검증의 핵심은 "이 정보로 제안사 또는 참여인력을 특정할 수 있는가"이다.
아래 우선순위 순서대로 판정하라.

【우선순위 1 — 사전 등록 실명 (절대 위반)】
- 제공된 "참여인력/대표자 실명 목록"에 있는 이름이 이미지 어디에든 보이면 → 무조건 즉시 【위반】
- 글자 사이 공백·점·기호가 있어도(예: 홍 길 동, 홍·길·동, 홍_길동) 동일 이름으로 판단
- 익명처리 여부, 맥락 무관 — 사전 이름이 한 글자라도 보이면 위반

【우선순위 2 — 이름+신원정보 조합 (위반)】
사전에 없는 이름이라도 아래 조합이 보이면 → 【위반】
- 2~4글자 한글 이름 + 직책(기술사·감리사·감리원·책임자·PM·PL·과장·부장·차장·전문가·컨설턴트 등)
- 2~4글자 한글 이름 + 자격증·학력·경력 정보
- 2~4글자 한글 이름 + "/" 또는 "·" 구분자 + 직책 (예: "홍길동 / 수석감리원")
- 프로필 카드·인력 소개 박스·조직도에서 발견된 한글 이름
- 이름처럼 보이는 한글 텍스트 옆에 경력/자격 설명이 붙은 경우

【우선순위 3 — 업체 식별 정보 (위반)】
- 사전 등록 회사명·영문명·약칭·도메인·이메일·브랜드명
- ㈜, 주식회사, 유한회사 등이 붙은 법인명 패턴
- 회사 도메인이 포함된 URL (예: https://github.com/회사명/...)
- 제안사 로고 또는 CI/BI

【우선순위 4 — 인물 사진】

━━━ 얼굴 사진 판정 기준 (오탐 방지) ━━━

【실제 인물 사진 → 위반】
다음 조건 중 하나 이상이 명확히 보이면 위반:
- 피부색이 자연스럽게 표현된 얼굴
- 눈·코·입 등 이목구비가 명확하게 구분됨
- 사람의 머리카락·피부 질감이 사진처럼 표현됨
- 상반신 또는 얼굴이 사진 형태로 촬영된 이미지
- 명함 사진 또는 프로필 사진 형태
- 여러 인물의 얼굴 사진이 배열된 경우
→ 위 조건이 충족되면 반드시 【위반】
→ 사진만 있고 이름이 마스킹된 경우도 실사 사진 자체는 위반

【인물 사진이 아닌 경우 → 허용】
다음 경우는 절대 위반으로 판정하지 않는다:
- 단색 실루엣 (검은색·회색·파란색 등)
- 픽토그램·아이콘·일러스트
- 만화체 캐릭터
- 사람 모양 도형
- 시스템 다이어그램·플로우차트·아키텍처 안에 포함된 사람 아이콘
- 얼굴이 흐릿하여 이목구비가 식별되지 않는 이미지
- 얼굴 크기가 매우 작아 식별 불가능한 경우
- 얼굴이 아닌 사람의 뒷모습 또는 먼 거리 인물
- AI 생성 일러스트 스타일 인물
→ 위 경우는 반드시 【허용】

【판단 기준 질문 — 반드시 스스로에게 물을 것】
"이 이미지에서 실제 사람의 얼굴(피부색 + 이목구비)을 식별할 수 있는가?"
→ YES (80% 이상 확신) : 실제 인물 사진 → 【위반】
→ NO 또는 확신도 80% 미만 : 아이콘·일러스트·실루엣 또는 식별 불가 → 반드시 【허용】

【우선순위 4-B — 회사 로고 (위반)】
- 페이지 어디에든 제안사 로고·CI·BI가 보이면 → 【위반】
- 특히 페이지 우측하단 코너: 슬라이드 마스터에 고정된 로고가 있는 경우가 많음
  → 우측하단 영역을 반드시 확인하라

★ 로고 레퍼런스 이미지가 제공된 경우 — 엄격한 판정 기준 ★
- 레퍼런스 로고와 형태(심볼+텍스트 구성), 색상, 폰트 스타일이 모두 명확히 일치해야만 위반
- 단순히 '비슷해 보이는' 도형, 아이콘, 배지, UI 요소는 위반 아님
- 확신도 80% 미만이면 위반이 아닌 【허용】으로 판정하라
- 발주기관(공공기관) 로고(예: 국가철도공단 KR, 행정안전부 등)는 절대 위반 아님 — 허용
- 제안서 내 스크린샷·시스템 UI 이미지 안에 포함된 로고 형태 요소도 위반 아님

- 로고 레퍼런스가 없는 경우: 회사명이 로고 형태(도형+텍스트, 특수 폰트 등)로 보이면 위반
- content 필드에 로고 위치도 명시 (예: "악티보 로고 (우측하단)")
- 워터마크·CI 색상 블록·슬로건이 함께 있는 로고 구성도 위반

【우선순위 5 — 간접 식별 정보 (주의)】
- 특정 업체만 사용하는 내부 솔루션명·슬로건·색상명 추정
- 확정할 수 없으나 특정 업체를 유추할 수 있는 고유 표현

━━━ 익명 처리 인정 기준 ━━━
- OOO, ○○○, ***, 홍○○, 홍길○, *000, 000, 홍*동 등 마스킹 기호로 이름이 대체된 경우 → 익명 인정
- "*000 (수석감리원)" 처럼 이름 마스킹 + 직책만 표기 → 【허용】
- 단, 사전 실명이 같은 페이지 어디에든 한 번이라도 노출되면 → 마스킹 여부 무관하게 위반
- "전체적으로 OOO 처리됐다"는 요약 판단 금지 — 각 이름을 개별 확인

━━━ 절대 위반 처리 금지 (허용) ━━━
발주기관명·로고, 실적 발주처명, 공공기관명(행정안전부·한국전력·금감원·국가철도공단·LH·도로공사 등),
사업명, 일반 기술·제품·오픈소스명, 아이콘·일러스트·픽토그램,
제안서 본문 내 스크린샷·UI 캡처 이미지 안에 포함된 요소,
레퍼런스 로고와 80% 미만 유사도로 확신하기 어려운 로고 형태

━━━ 2글자 이름 오탐 방지 규칙 (매우 중요) ━━━
실명 목록에 2글자 이름(예: 국민, 전일, 서민 등)이 있더라도 아래 경우는 반드시 【허용】으로 판정하라:

【허용 케이스 — 기관명/복합어 일부】
- 이름이 기관명·단체명의 일부로 사용된 경우
  예: "국민건강보험공단", "대국민 서비스", "국민안전처", "서민금융진흥원"
  → "국민", "서민" 이 기관명에 포함된 것이므로 인명이 아님 → 【허용】
- 이름 앞뒤로 한글이 끊김 없이 이어지는 복합어: 예) "전일제", "전일근무", "전일 작업"
  → 직무·업무 용어이므로 인명 아님 → 【허용】
- 일반 사회적 용어: "국민", "시민", "서민"이 단독으로 일반 맥락에서 사용된 경우
  예: "국민 편의 증진", "시민 서비스"

【위반 케이스 — 실제 인명 맥락】
- 이름 뒤에 직책/직위가 붙은 경우: "국민 수석감리원", "전일 PM", "서민 책임자"
- 인력 소개표·조직도·명함 형태에 2글자 이름이 단독으로 등장
- "참여인력:", "담당자:", "PM:", "PL:" 등의 라벨 뒤에 2글자 이름이 오는 경우

판단 기준: "이 2글자가 사람 이름으로 사용되었는가, 아니면 기관명/일반어의 일부인가?"를 이미지 전체 맥락으로 판단하라.

━━━ 판정 요약표 ━━━
【위반】사전 실명 / 이름+직책 조합 / 업체명·로고·도메인·이메일 / 실사 인물 사진 / 워터마크·명함 / 우측하단 회사 로고
【주의】간접 식별 가능 고유 표현 / 확정 불가 브랜드명 / 실제 사람인지 불명확한 실사 이미지
【허용】발주기관 정보 / 공공기관명 / 완전 마스킹된 인력 정보 / 일반 기술명 / 아이콘 / 실루엣·픽토그램·일러스트

━━━ 페이지별 체크리스트 (매 페이지 반드시 확인) ━━━
□ 우측 하단 코너에 로고가 있는가? (슬라이드 마스터 로고)
□ 인물 사진(프로필, 명함, 상반신)이 있는가?
□ 이름+직책/자격이 같이 있는가?
□ 회사명·영문명·약칭이 텍스트로 보이는가?
□ 이메일·URL·도메인이 있는가?

━━━ 출력 원칙 (매우 중요) ━━━
- 위반/주의 요소가 여러 개면 각각 별도 items 항목으로 출력 (묶기·생략 금지)
- 업체명·인력명·이메일·사진 등 유형이 다른 위반은 반드시 분리 출력
- 같은 페이지에 동일 유형 위반이 N건이면 N개 항목 출력
- 이름+직책이 같은 줄에 있으면 → "인력명+직책" 1개 항목으로 통합 출력 가능

━━━ 텍스트 추출 탐지 결과 처리 ━━━
- 이미지 앞에 "[N페이지 텍스트 레이어에서 이미 탐지된 항목]"이 있으면, 해당 항목들을 JSON에 그대로 포함
- 단, 해당 항목은 명시된 N페이지에만 귀속시킬 것. 같은 배치의 다른 페이지에 적용하지 말 것
- 텍스트 탐지 결과가 없는 페이지도 이미지를 꼼꼼히 확인

━━━ 마스킹 처리된 이름 인식 ━━━
- "O O O", "○○○", "OOO", "***", "□□□" 등의 패턴은 이미 마스킹 처리된 것 → 무조건 【허용】
- 마스킹된 이름 뒤에 직책(수석감리원, PM, PL 등)이 붙어 있어도 이름이 마스킹됐으면 → 【허용】
- "?? ??" 패턴도 마스킹 처리된 것으로 판단 → 【허용】

━━━ 절대 금지 ━━━
- 사전 실명이 보이는데 허용·생략하는 것
- "전반적으로 OOO 처리됐다"는 일괄 요약 판단
- 배치 내 다른 페이지를 근거로 이 페이지도 익명처리됐다고 가정하는 것
- 레퍼런스 로고와 확신 없이 "유사해 보인다"는 이유만으로 위반 판정하는 것
- 공공기관·발주처 로고를 제안사 로고로 오인하여 위반 판정하는 것
- 단색 실루엣·픽토그램·아이콘·도형을 인물 사진으로 오인하여 위반 판정하는 것
- 특정인을 식별할 수 없는 이미지를 인물 사진 위반으로 판정하는 것
- 얼굴이 명확히 보이지 않는 이미지를 인물사진으로 판정하는 것
- 텍스트 힌트에서 탐지된 항목을 다른 페이지 번호로 출력하는 것 (반드시 힌트에 명시된 페이지 번호로만 출력)

반드시 아래 JSON 형식으로만 반환하라. 다른 텍스트 절대 포함 금지:
{
  "items": [
    {
      "page": "페이지 번호 (단일 숫자만, 예: 5)",
      "type": "검출 유형",
      "content": "검출 내용 (사전 이름이면 해당 이름 명시)",
      "judgment": "위반 또는 주의 또는 허용",
      "reason": "판정 사유",
      "recommendation": "수정 권고 (허용이면 없음)"
    }
  ]
}
- 문제 없는 페이지는 포함하지 않아도 된다. 단, 텍스트 탐지에서 위반이 확인된 페이지는 반드시 포함하라.
- 한 페이지에 위반 요소가 N개면 items 배열에 N개 항목이 있어야 한다. 누락 금지."""


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
                "text": "아래는 제안사 공식 로고 레퍼런스 이미지이다.\n판정 기준: 이 로고와 형태(심볼+워드마크 구성), 색상, 폰트 스타일이 모두 명확히 일치하는 경우에만 위반으로 판정하라.\n단순히 비슷해 보이는 도형·아이콘·배지·UI 요소는 위반 아님. 확신도 80% 미만이면 허용으로 판정하라.\n발주기관(공공기관) 로고는 이 레퍼런스와 무관하게 절대 위반 아님."
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
                hint_lines = [f"[{pg['page']}페이지 텍스트 레이어에서 이미 탐지된 항목 — 아래 항목을 page: \"{pg['page']}\" 로 JSON에 포함할 것]"]
                for h in violations:
                    c = h.get('content', '')
                    t = h.get('type', '')
                    hint_lines.append(f'  - page: "{pg["page"]}", type: "{t}", content: "{c}", judgment: "위반"')
                for h in cautions:
                    c = h.get('content', '')
                    t = h.get('type', '')
                    hint_lines.append(f'  - page: "{pg["page"]}", type: "{t}", content: "{c}", judgment: "주의"')
                hint_lines.append(f"※ 위 항목들은 {pg['page']}페이지 텍스트 추출로 확인된 값입니다. page 번호를 반드시 {pg['page']}로 출력하고, content 값을 그대로 JSON에 출력하십시오. 다른 페이지 번호로 출력하지 마십시오.")
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
                max_tokens=8192,
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
