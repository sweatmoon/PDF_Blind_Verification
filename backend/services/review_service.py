"""
제안서 검수 서비스
- 4개 파일(감리RFP, 대상사업RFP, 포털HTML, 제안서PPT) → Claude Sonnet 분석 → JSON 리포트
"""
from __future__ import annotations
import io
import json
import base64
import re
from pathlib import Path
from typing import Optional

from core.config import get_logger, ANTHROPIC_API_KEY, DATA_DIR

logger = get_logger("review_service")

# 검수 전용 모델 (관리자에서 변경 가능)
_REVIEW_MODEL_FILE = DATA_DIR / "review_model.txt"
DEFAULT_REVIEW_MODEL = "claude-sonnet-4-5"


def get_review_model() -> str:
    try:
        if _REVIEW_MODEL_FILE.exists():
            m = _REVIEW_MODEL_FILE.read_text("utf-8").strip()
            if m:
                # 잘못된 날짜 suffix 자동 보정 (예: claude-sonnet-4-5-20250514 → claude-sonnet-4-5)
                if m == "claude-sonnet-4-5-20250514":
                    m = "claude-sonnet-4-5"
                    _REVIEW_MODEL_FILE.write_text(m, "utf-8")
                    logger.info(f"[review] 잘못된 모델 ID 자동 보정: {m}")
                return m
    except Exception:
        pass
    return DEFAULT_REVIEW_MODEL


def set_review_model(model: str):
    try:
        _REVIEW_MODEL_FILE.write_text(model.strip(), "utf-8")
    except Exception as e:
        logger.warning(f"review model 저장 실패: {e}")


def _extract_text_from_hwp(data: bytes) -> str:
    """HWP 5.x (OLE 컨테이너) → 텍스트 추출"""
    try:
        import olefile
        import zlib
        import struct

        ole = olefile.OleFileIO(io.BytesIO(data))

        # FileHeader에서 압축 여부 플래그 확인 (offset 36, bit0)
        try:
            hdr = ole.openstream("FileHeader").read()
            is_compressed = bool(struct.unpack_from("<I", hdr, 36)[0] & 1)
        except Exception:
            is_compressed = True

        texts: list[str] = []
        for i in range(1, 300):
            stream_name = f"BodyText/Section{i:04d}"
            if not ole.exists(stream_name):
                break
            raw = ole.openstream(stream_name).read()
            if is_compressed:
                try:
                    raw = zlib.decompress(raw, -15)
                except Exception:
                    pass

            # HWP 레코드 스트림 파싱 — PARA_TEXT (type=67) 레코드만 추출
            pos = 0
            while pos + 4 <= len(raw):
                hword = struct.unpack_from("<I", raw, pos)[0]
                rec_type = hword & 0x3FF
                rec_size = (hword >> 20) & 0xFFF
                if rec_size == 0xFFF:
                    if pos + 8 > len(raw):
                        break
                    rec_size = struct.unpack_from("<I", raw, pos + 4)[0]
                    pos += 8
                else:
                    pos += 4
                payload = raw[pos: pos + rec_size]
                pos += rec_size

                if rec_type == 67:  # PARA_TEXT
                    try:
                        chars: list[str] = []
                        for j in range(0, len(payload) - 1, 2):
                            code = struct.unpack_from("<H", payload, j)[0]
                            if code in (0x000D, 0x0000):
                                chars.append("\n")
                            elif code >= 0x0020:
                                chars.append(chr(code))
                        line = "".join(chars).strip()
                        if line:
                            texts.append(line)
                    except Exception:
                        pass
        ole.close()
        result = "\n".join(texts)
        logger.debug(f"[review] HWP 추출: {len(result):,}자")
        return result
    except Exception as e:
        logger.warning(f"[review] HWP 텍스트 추출 실패: {e}")
        return ""


def _extract_text_from_hwpx(data: bytes) -> str:
    """HWPX (ZIP+XML 컨테이너) → 텍스트 추출"""
    try:
        import zipfile
        from lxml import etree

        texts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Contents/section0.xml, section1.xml, ...
            section_files = sorted(
                n for n in zf.namelist()
                if n.startswith("Contents/section") and n.endswith(".xml")
            )
            for fname in section_files:
                xml_bytes = zf.read(fname)
                try:
                    root = etree.fromstring(xml_bytes)
                    for elem in root.iter():
                        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                        if tag == "t" and elem.text and elem.text.strip():
                            texts.append(elem.text.strip())
                except Exception:
                    pass
        result = "\n".join(texts)
        logger.debug(f"[review] HWPX 추출: {len(result):,}자")
        return result
    except Exception as e:
        logger.warning(f"[review] HWPX 텍스트 추출 실패: {e}")
        return ""


def _extract_text_from_pdf(data: bytes) -> str:
    """PDF → 텍스트 추출 (pdfplumber 우선, 실패 시 PyPDF2)"""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"[페이지 {i+1}]\n{text}")
            return "\n\n".join(pages)
    except Exception as e1:
        logger.debug(f"pdfplumber 실패: {e1}")
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(data))
            pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"[페이지 {i+1}]\n{text}")
            return "\n\n".join(pages)
        except Exception as e2:
            logger.warning(f"PDF 텍스트 추출 실패: {e2}")
            return ""


def _extract_text_from_pptx(data: bytes) -> str:
    """PPTX → 텍스트 추출 (슬라이드별)"""
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(data))
        slides = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                try:
                    if shape.has_text_frame:
                        t = shape.text_frame.text.strip()
                        if t:
                            texts.append(t)
                    # 표
                    if shape.has_table:
                        for row in shape.table.rows:
                            row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                            if row_texts:
                                texts.append(" | ".join(row_texts))
                except Exception:
                    pass
            if texts:
                slides.append(f"[슬라이드 {i}]\n" + "\n".join(texts))
        return "\n\n".join(slides)
    except Exception as e:
        logger.warning(f"PPTX 텍스트 추출 실패: {e}")
        return ""


def _extract_text_from_html(data: bytes) -> str:
    """HTML → 텍스트 추출"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(data, "html.parser")
        # script/style 제거
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception:
        # fallback: 정규식
        text = data.decode("utf-8", errors="ignore")
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", "\n", text).strip()


def _truncate(text: str, max_chars: int = 40000) -> str:
    """토큰 초과 방지용 텍스트 절삭"""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n\n... [중간 {len(text)-max_chars:,}자 생략] ...\n\n" + text[-half:]


_SYSTEM_PROMPT = """\
당신은 입찰 제안서 전문 검수 AI입니다.
사용자가 제공하는 4개 문서를 분석하여 정해진 JSON 스키마를 정확히 출력합니다.

## 절대 규칙
1. 응답은 반드시 유효한 JSON 객체 **하나만** 출력한다. 코드블록(```), 설명 문구, 마크다운 일절 금지.
2. JSON 문자열 값 안에 실제 줄바꿈 문자(0x0A/0x0D)를 절대 사용하지 않는다. 줄바꿈이 필요하면 반드시 \\n 이스케이프 시퀀스를 사용한다.
3. 모든 문자열 내 이중인용부호(")는 반드시 \\" 로 이스케이프한다.
4. 배열·객체 값이 없을 때는 null 대신 빈 배열 [] 또는 빈 문자열 ""을 사용한다.

## 출력 JSON 스키마 (키와 타입을 정확히 지킬 것)
{
  "id": "string — 영문소문자-숫자-하이픈 슬러그",
  "name": "string — 사업명(PPT 표지 기준)",
  "org": "string — 발주기관명",
  "date": "string — 검수일",
  "counts": {"crit": 0, "major": 0, "minor": 0, "check": 0},
  "verdict": "string — 총평. <b>강조</b> HTML 태그 사용 가능. 줄바꿈은 \\n",
  "overview": [["사업명","RFP기준","포털기준","PPT표기","ok|major|crit"], ...],
  "baseline": [["항목명","기준값","출처"], ...],
  "scopeCoverage": [{"requirement":"string","coveredInPPT":true,"ppSlide":"string","note":"string"}, ...],
  "critical": [{"title":"string","slide":"string","fix":"string","body":"string"}, ...],
  "major":    [{"title":"string","slide":"string","fix":"string","body":"string"}, ...],
  "minor":    [{"title":"string","slide":"string","fix":"string","body":"string"}, ...],
  "checkNeeded": [{"title":"string","slide":"string","fix":"string","body":"string"}, ...],
  "schedule": [["단계명","HTML일정","PPT일정","HTML MD","PPT MD","ok|major|check|crit"], ...],
  "scheduleNote": "string",
  "personnel": [["구분","HTML기준인력","PPT표기인력","ok|major|check|crit"], ...],
  "qualificationCheck": [{"person":"string","requirement":"string","actual":"string","meets":true,"note":"string"}, ...],
  "costCheck": [["항목","RFP/포털기준","PPT표기","ok|major|check"], ...],
  "deliverableCheck": [["산출물명","RFP기한","PPT기한","ok|major|check"], ...],
  "irrelevant": {"summary":"string","items":[{"text":"string","slide":"string","sourceGuess":"string","severity":"high|low"}]},
  "typoChecklist": [{"no":1,"slide":"string","type":"string","priority":"높음|중간|낮음","original":"string","fix":"string","note":"string"}, ...],
  "typoNote": "string",
  "priority": {"crit":["string"],"major":["string"],"check":["string"]}
}"""


def _build_messages(
    audit_rfp: str,
    target_rfp: str,
    portal_html: str,
    proposal_ppt: str,
    today: str,
) -> list[dict]:
    """system/user/assistant 메시지 배열 반환 (prefill 포함)"""
    user_content = f"""오늘 날짜: {today}

아래 4개 문서를 바탕으로 정성제안서 PPT를 검수하여 JSON을 출력하라.

## 검수 지침

### [1] 사업 개요 정합성 → overview
- 사업명 / 발주기관 / 사업기간을 각각 개별 행으로 3개 문서와 비교
- 글자 단위 차이, 괄호·부제 포함 여부까지 확인

### [2] 제안 범위 커버리지 → scopeCoverage
- RFP 과업범위 항목을 하나씩 추출 후 PPT 반영 여부 대조
- 미커버(coveredInPPT:false) → major 이상 등재

### [3] 가격·비용 대조 → costCheck
- 제안 총액, 부가세 포함/제외 기준, MD 단가×총MD 재계산
- 가격 정보 미확보 시 [] 로 두고 checkNeeded에 "가격 정보 미확인" 등재

### [4] 기술 자격 요건 → qualificationCheck
- RFP 감리원 자격 기준을 인원별로 대조
- meets:false → critical 즉시 등재

### [5] 잔존 문구 검출 → irrelevant
- 다른 사업 복붙 의심 문구를 슬라이드별로 열거
- 발견 없어도 summary는 반드시 작성

### [6] 납기·납품물 대조 → deliverableCheck
- RFP 요구 산출물과 제출기한을 PPT와 대조

### [7] 일정·공수 대조 → schedule + scheduleNote
### [8] 인력명 대조 → personnel
### [9] 오타·표기 일관성 → typoChecklist + typoNote
### [10] 기준 정보 요약 → baseline

counts는 critical/major/minor/checkNeeded 배열의 실제 길이로 계산.
verdict는 검수 총평 (중요 표현 <b>굵게</b>, 줄바꿈 \\n).

---

[감리사업 RFP]
{_truncate(audit_rfp, 30000)}

---

[대상사업 RFP]
{_truncate(target_rfp, 30000)}

---

[포털 제안작업표 HTML]
{_truncate(portal_html, 25000)}

---

[정성제안서 PPT]
{_truncate(proposal_ppt, 50000)}
"""
    return [
        {"role": "user",      "content": user_content},
        {"role": "assistant", "content": "{"},   # prefill — JSON { 로 시작 강제
    ]


def run_review(
    audit_rfp_data: bytes,   audit_rfp_name: str,
    target_rfp_data: bytes,  target_rfp_name: str,
    portal_html_data: bytes, portal_html_name: str,
    proposal_ppt_data: bytes, proposal_ppt_name: str,
    api_key: str = "",
) -> dict:
    """
    4개 파일을 Claude Sonnet으로 분석하여 검수 JSON 반환.
    """
    key = api_key or ANTHROPIC_API_KEY
    if not key:
        raise ValueError("Claude API 키가 설정되지 않았습니다.")

    # ── 텍스트 추출 ──────────────────────────────────────────────
    logger.info(f"[review] 텍스트 추출 시작: {audit_rfp_name}, {target_rfp_name}, {portal_html_name}, {proposal_ppt_name}")

    def extract(data: bytes, name: str) -> str:
        ext = Path(name).suffix.lower()
        if ext == ".pdf":
            return _extract_text_from_pdf(data)
        elif ext in (".pptx", ".ppt"):
            return _extract_text_from_pptx(data)
        elif ext in (".html", ".htm"):
            return _extract_text_from_html(data)
        elif ext == ".hwpx":
            return _extract_text_from_hwpx(data)
        elif ext == ".hwp":
            return _extract_text_from_hwp(data)
        else:
            # 텍스트/마크다운/docx 등
            return data.decode("utf-8", errors="ignore")

    audit_rfp_text   = extract(audit_rfp_data,   audit_rfp_name)
    target_rfp_text  = extract(target_rfp_data,  target_rfp_name)
    portal_html_text = extract(portal_html_data, portal_html_name)
    proposal_ppt_text = extract(proposal_ppt_data, proposal_ppt_name)

    logger.info(
        f"[review] 추출 완료: 감리RFP={len(audit_rfp_text):,}자 "
        f"대상RFP={len(target_rfp_text):,}자 "
        f"포털={len(portal_html_text):,}자 "
        f"PPT={len(proposal_ppt_text):,}자"
    )

    # ── 날짜 ─────────────────────────────────────────────────────
    from core.config import now_kst
    today = now_kst().strftime("%Y.%m.%d")

    # ── 메시지 구성 ───────────────────────────────────────────────
    messages = _build_messages(
        audit_rfp_text, target_rfp_text,
        portal_html_text, proposal_ppt_text,
        today,
    )

    # ── Claude API 호출 ──────────────────────────────────────────
    model = get_review_model()
    total_chars = sum(len(m["content"]) for m in messages)
    logger.info(f"[review] Claude 호출: model={model}, 총 입력={total_chars:,}자")

    import anthropic
    client = anthropic.Anthropic(api_key=key)

    message = client.messages.create(
        model=model,
        max_tokens=16000,          # 검수 JSON 전체 출력에 충분한 크기
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    # stop_reason 확인 — max_tokens로 잘린 경우 경고
    stop_reason = message.stop_reason
    if stop_reason == "max_tokens":
        logger.warning(f"[review] Claude 응답이 max_tokens에 의해 잘림! 일부 결과 누락 가능")

    # prefill "{" 포함해서 전체 JSON 복원
    raw_text = message.content[0].text if message.content else ""
    raw = "{" + raw_text          # prefill의 "{" 를 앞에 붙임
    logger.info(f"[review] Claude 응답: {len(raw_text):,}자 (stop={stop_reason}, prefill 포함 {len(raw):,}자)")

    # ── JSON 파싱 ────────────────────────────────────────────────
    # 혹시 코드블록으로 감싸진 경우 제거 후 파싱
    json_str = raw.strip()
    # ```json ... ``` 혹은 ``` ... ``` 제거
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", json_str)
    if m:
        json_str = "{" + m.group(1) if not m.group(1).startswith("{") else m.group(1)

    def _sanitize_json_strings(s: str) -> str:
        """JSON 문자열 값 안에 들어간 실제 제어문자(줄바꿈 등)를 이스케이프 시퀀스로 변환.
        Claude가 verdict/body 등 긴 문자열 안에 raw newline을 넣어
        JSONDecodeError: Invalid control character 가 발생하는 문제 수정."""
        result: list[str] = []
        in_string = False
        escaped = False
        for ch in s:
            if escaped:
                result.append(ch)
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                result.append(ch)
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                if ch == "\n":
                    result.append("\\n")
                elif ch == "\r":
                    result.append("\\r")
                elif ch == "\t":
                    result.append("\\t")
                else:
                    result.append(ch)
            else:
                result.append(ch)
        return "".join(result)

    def _try_parse(s: str) -> dict | None:
        """JSON 파싱 시도. 실패 시 None 반환."""
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    # 1차: 원본 파싱
    result = _try_parse(json_str)

    # 2차: 제어문자 이스케이프 후 파싱
    if result is None:
        sanitized = _sanitize_json_strings(json_str)
        result = _try_parse(sanitized)
        if result is not None:
            logger.info("[review] JSON sanitize 후 파싱 성공")

    # 3차: 마지막 완결 `}` 까지 잘라서 파싱 (truncation 대비)
    if result is None:
        last_brace = json_str.rfind("}")
        if last_brace > 0:
            result = _try_parse(json_str[:last_brace + 1])
            if result is None:
                result = _try_parse(_sanitize_json_strings(json_str[:last_brace + 1]))
            if result is not None:
                logger.info("[review] 말미 잘라내기 후 파싱 성공")

    if result is None:
        logger.warning(
            f"[review] JSON 파싱 최종 실패 (stop={stop_reason}).\n"
            f"  응답 처음 1000자: {raw[:1000]}\n"
            f"  응답 끝  500자: {raw[-500:]}"
        )
        result = {
            "id": "parse-error",
            "name": "파싱 오류",
            "org": "",
            "date": f"{today} 검수",
            "counts": {"crit": 0, "major": 0, "minor": 0, "check": 0},
            "verdict": (
                "JSON 파싱에 실패했습니다. "
                + ("응답이 너무 길어 잘렸습니다(max_tokens 초과). " if stop_reason == 'max_tokens' else "")
                + "파일을 다시 업로드하거나 관리자에게 문의하세요."
            ),
            "baseline": [],
            "critical": [], "major": [], "minor": [], "checkNeeded": [],
            "schedule": [], "scheduleNote": "",
            "personnel": [],
            "irrelevant": {"summary": "파싱 오류로 분석 불가", "items": []},
            "typoChecklist": [], "typoNote": "",
            "priority": {"crit": [], "major": [], "check": []},
            "_debug_raw": raw[:3000],   # 개발자 디버깅용 (첫 3000자)
            "_debug_tail": raw[-1000:], # 개발자 디버깅용 (마지막 1000자)
            "_stop_reason": stop_reason,
        }

    # ── 신규 필드 기본값 보정 ─────────────────────────────────────
    result.setdefault("overview", [])
    result.setdefault("scopeCoverage", [])
    result.setdefault("qualificationCheck", [])
    result.setdefault("costCheck", [])
    result.setdefault("deliverableCheck", [])
    # irrelevant: 문자열이면 신규 구조로 변환
    irr = result.get("irrelevant", "")
    if isinstance(irr, str):
        result["irrelevant"] = {
            "summary": irr if irr else "검출 결과 없음",
            "items": [],
        }
    else:
        result["irrelevant"].setdefault("summary", "")
        result["irrelevant"].setdefault("items", [])

    # counts 자동 계산 (Claude가 빠뜨린 경우 보정)
    result.setdefault("counts", {})
    result["counts"]["crit"]  = len(result.get("critical", []))
    result["counts"]["major"] = len(result.get("major", []))
    result["counts"]["minor"] = len(result.get("minor", []))
    result["counts"]["check"] = len(result.get("checkNeeded", []))

    return result
