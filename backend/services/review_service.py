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
DEFAULT_REVIEW_MODEL = "claude-sonnet-4-5-20250514"


def get_review_model() -> str:
    try:
        if _REVIEW_MODEL_FILE.exists():
            m = _REVIEW_MODEL_FILE.read_text("utf-8").strip()
            if m:
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


def _build_prompt(
    audit_rfp: str,
    target_rfp: str,
    portal_html: str,
    proposal_ppt: str,
    today: str,
) -> str:
    return f"""아래 4개 문서를 첨부합니다.

첨부 파일:
- 감리사업 제안요청서 (RFP)
- 대상사업 제안요청서 (RFP)
- 포털 제안작업표 HTML
- 정성제안서 최종본 PPT

---

## 지시

정성제안서 PPT를 감리사업 RFP, 대상사업 RFP, 포털 제안작업표 HTML과 대조하여 정합성을 검수하고,
아래 JSON 스키마를 **완전히** 준수하는 JSON 객체 하나를 출력하라.
JSON 외에 다른 텍스트는 절대 출력하지 않는다. 코드블록(```json ... ```)으로 감싸서 출력한다.

오늘 날짜: {today}

---

## 출력 JSON 스키마

```json
{{
  "id": "영문소문자-숫자-하이픈 조합 슬러그 (예: kostat-bigdata-2026)",
  "name": "사업명 (PPT 표지 기준)",
  "org": "발주기관명",
  "date": "{today} 검수",
  "counts": {{
    "crit": 0,
    "major": 0,
    "minor": 0,
    "check": 0
  }},
  "verdict": "총평 HTML 문자열. 중요 표현은 <b>굵게</b> 처리. 줄바꿈은 \\n.",
  "baseline": [
    ["항목명", "기준값 (RFP/포털 확정값)", "출처 (예: 감리사업 RFP / 포털 HTML / 대상사업 RFP)"]
  ],
  "critical": [
    {{
      "title": "오류 제목 (한 줄, 명확하게)",
      "slide": "Slide 번호 (예: Slide 3)",
      "fix": "수정 필요값 — plain text",
      "body": "상세 설명 HTML. <p>, <div class=\\"calc\\">, <div class=\\"kv\\">, <table> 사용 가능."
    }}
  ],
  "major": [],
  "minor": [],
  "checkNeeded": [],
  "schedule": [
    ["단계명", "HTML 기준 일정", "PPT 표기 일정", "HTML 기준 공수(MD)", "PPT 표기 공수(MD)", "ok | major | check | crit"]
  ],
  "scheduleNote": "일정·공수 대조 전체 요약",
  "personnel": [
    ["구분", "HTML 기준 인력", "PPT 표기 인력", "ok | major | check | crit"]
  ],
  "irrelevant": "본사업과 무관한 잔존 문구 검출 결과",
  "typoChecklist": [
    {{
      "no": 1,
      "slide": "슬라이드 번호",
      "type": "오타 | 일관성 | 수치 | 맥락(잔존문구)",
      "priority": "높음 | 중간 | 낮음",
      "original": "원문 발췌",
      "fix": "수정안",
      "note": "설명·근거"
    }}
  ],
  "typoNote": "오타 검수 전체 요약",
  "priority": {{
    "crit": ["치명 항목별 한 줄 수정 지시"],
    "major": ["중대 항목별 한 줄 수정 지시"],
    "check": ["확인 필요 항목별 한 줄 확인 지시"]
  }}
}}
```

---

## 문서 내용

### [감리사업 RFP]
{_truncate(audit_rfp, 35000)}

---

### [대상사업 RFP]
{_truncate(target_rfp, 35000)}

---

### [포털 제안작업표 HTML]
{_truncate(portal_html, 25000)}

---

### [정성제안서 PPT]
{_truncate(proposal_ppt, 50000)}
"""


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

    # ── 프롬프트 구성 ─────────────────────────────────────────────
    prompt = _build_prompt(
        audit_rfp_text, target_rfp_text,
        portal_html_text, proposal_ppt_text,
        today,
    )

    # ── Claude API 호출 ──────────────────────────────────────────
    model = get_review_model()
    logger.info(f"[review] Claude 호출: model={model}, prompt_len={len(prompt):,}자")

    import anthropic
    client = anthropic.Anthropic(api_key=key)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text if message.content else ""
    logger.info(f"[review] Claude 응답: {len(raw):,}자")

    # ── JSON 파싱 ────────────────────────────────────────────────
    # ```json ... ``` 블록 추출
    m = re.search(r"```json\s*([\s\S]+?)\s*```", raw)
    json_str = m.group(1) if m else raw.strip()

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"[review] JSON 파싱 실패: {e}. raw[:500]={raw[:500]}")
        # 부분 복구 시도
        result = {
            "id": "parse-error",
            "name": "파싱 오류",
            "org": "",
            "date": f"{today} 검수",
            "counts": {"crit": 0, "major": 0, "minor": 0, "check": 0},
            "verdict": f"<p>JSON 파싱 오류가 발생했습니다. Claude 응답 원문:</p><pre style='font-size:11px;white-space:pre-wrap'>{raw[:3000]}</pre>",
            "baseline": [],
            "critical": [], "major": [], "minor": [], "checkNeeded": [],
            "schedule": [], "scheduleNote": "",
            "personnel": [],
            "irrelevant": "",
            "typoChecklist": [], "typoNote": "",
            "priority": {"crit": [], "major": [], "check": []},
        }

    # counts 자동 계산 (Claude가 빠뜨린 경우 보정)
    result.setdefault("counts", {})
    result["counts"]["crit"]  = len(result.get("critical", []))
    result["counts"]["major"] = len(result.get("major", []))
    result["counts"]["minor"] = len(result.get("minor", []))
    result["counts"]["check"] = len(result.get("checkNeeded", []))

    return result
