"""
서버사이드 완전 검증 파이프라인
- 브라우저 없이 서버에서 PDF→이미지 변환→Claude Vision 분석 수행
- 대시보드 모드: 업로드 후 브라우저 닫아도 백그라운드에서 완료됨
- OCR 연동: 텍스트 레이어 없는 이미지 전용 페이지도 자동 OCR 후 규칙 탐지
"""
from __future__ import annotations
import asyncio, base64, io, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from services.rule_detector import get_rule_detector, _is_org_context
from services.claude_judge  import get_claude_judge, ClaudeVisionJudge
from services.ocr_service   import get_ocr
from services.file_manager  import _wipe_file
from services.pdf_service   import PDFService
from core.config import get_logger, update_job, load_dict, DATA_DIR

logger = get_logger("server_pipeline")

PAGES_PER_BATCH    = 1    # Claude Vision 배치당 페이지 수 (1=오탐 방지)
RENDER_DPI         = 200  # Claude Vision용 이미지 DPI (120=252KB/장, 200=539KB/장, 300=939KB/장)
OCR_DPI            = 120  # OCR용 이미지 DPI (GV는 120으로 충분, 1300px)
GV_BATCH_SIZE      = 16   # Google Vision 배치당 최대 페이지 수
MAX_PAGES          = 200  # 최대 처리 페이지 수
OCR_TEXT_THRESHOLD = 50   # 이 글자 수 미만이면 OCR 실행
_executor = ThreadPoolExecutor(max_workers=8)


# ── 페이지 렌더링 (Claude Vision용 JPEG base64) ───────────────────
def _render_page_to_b64(svc: PDFService, page_idx: int, dpi: int = RENDER_DPI) -> Optional[str]:
    """PyMuPDF로 페이지를 JPEG base64로 변환"""
    try:
        import fitz
        doc = fitz.open(str(svc.path))
        page = doc[page_idx]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img_bytes = pix.tobytes("jpeg", jpg_quality=75)
        doc.close()
        return base64.b64encode(img_bytes).decode("utf-8")
    except Exception as e:
        logger.warning(f"페이지 {page_idx+1} 렌더링 실패: {e}")
        return None


# ── 페이지 렌더링 → PIL Image (OCR/GV용) ────────────────────────
def _render_page_to_pil(pdf_path: Path, page_idx: int, dpi: int = OCR_DPI):
    """PyMuPDF로 페이지를 PIL Image로 변환 (OCR 입력용)"""
    try:
        import fitz
        from PIL import Image as PILImage
        doc = fitz.open(str(pdf_path))
        page = doc[page_idx]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        img_bytes = pix.tobytes("jpeg", jpg_quality=85)
        doc.close()
        return PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception as e:
        logger.warning(f"페이지 {page_idx+1} PIL 렌더링 실패: {e}")
        return None


# ── Tesseract 폴백 단일 OCR (ThreadPool용) ───────────────────────
def _tesseract_ocr_page(pdf_path: Path, page_idx: int) -> str:
    """Tesseract로 단일 페이지 OCR (GV 실패 폴백용)"""
    try:
        ocr = get_ocr()
        img = _render_page_to_pil(pdf_path, page_idx, dpi=OCR_DPI)
        if img is None:
            return ""
        # 전체 OCR
        full_text = ocr._tesseract_ocr(img)
        # 우측하단 크롭 보완 (로고 영역)
        try:
            w, h = img.width, img.height
            region = img.crop((int(w * 0.72), int(h * 0.78), w, h))
            corner = ocr._tesseract_ocr(region)
            if corner.strip():
                existing = set(full_text.lower().split())
                extra = " ".join(t for t in corner.split()
                                 if t.lower() not in existing and len(t) >= 2)
                if extra:
                    full_text = full_text + "\n" + extra
        except Exception:
            pass
        return full_text
    except Exception as e:
        logger.warning(f"Tesseract OCR 페이지 {page_idx+1} 실패: {e}")
        return ""


class ServerPipeline:
    """서버사이드 완전 검증 파이프라인"""

    def __init__(self):
        self.rules = get_rule_detector()
        self.judge: ClaudeVisionJudge = get_claude_judge()
        self.ocr   = get_ocr()

    async def run(self, job_id: str, pdf_path: Path, filename: str) -> dict:
        """
        전체 검증 수행 후 report dict 반환
        브라우저 없이 서버에서 완전 처리
        """
        t0 = time.time()
        logger.info(f"[{job_id}] 서버 파이프라인 시작: {filename}")

        def prog(pct: int, msg: str):
            update_job(job_id, progress=pct, message=msg)

        loop = asyncio.get_event_loop()

        # ── 1. PDF 열기 & 기본 정보 ─────────────────────────────
        prog(5, "PDF 파싱 중…")
        svc = PDFService(pdf_path)
        if not svc.open():
            raise RuntimeError("PDF 파일을 열 수 없습니다.")

        total = min(svc.total_pages, MAX_PAGES)
        claude_on = self.judge.enabled and getattr(self.judge, '_client', None) is not None
        ocr_on    = self.ocr.enabled
        gv_on     = self.ocr.use_google_vision
        mode_desc = []
        if claude_on: mode_desc.append("Claude Vision")
        if ocr_on:    mode_desc.append("Google Vision OCR" if gv_on else "Tesseract OCR")
        mode_desc.append("규칙 탐지")
        logger.info(f"[{job_id}] 분석 모드: {' + '.join(mode_desc)} | 총 {total}p")
        prog(8, f"총 {svc.total_pages}페이지 확인 · 분석 준비 중…")

        # ── 2. 텍스트 추출 + OCR 병렬 실행 ─────────────────────
        #    텍스트 레이어가 부족한 페이지는 OCR로 보완
        prog(10, "텍스트 추출 중…")

        # 먼저 PyMuPDF 텍스트 추출 (빠름)
        raw_texts: dict[int, str] = {}        # idx → 텍스트
        ocr_needed: list[int]     = []         # OCR이 필요한 페이지 idx 목록

        import fitz as _fitz
        _doc_check = _fitz.open(str(pdf_path))
        for i in range(total):
            page     = svc.extract_page(i)
            raw_texts[i] = page.text
            if len(page.text.strip()) < OCR_TEXT_THRESHOLD:
                # 이미지가 있거나 페이지 자체 크기가 있으면 OCR 대상
                _pg   = _doc_check[i]
                _imgs = _pg.get_images()
                _rect = _pg.rect
                # 임베디드 이미지 있음 OR 페이지 넓이가 충분함(벡터 슬라이드)
                if _imgs or (_rect.width > 100 and _rect.height > 100):
                    ocr_needed.append(i)
        _doc_check.close()

        logger.info(f"[{job_id}] PyMuPDF 텍스트: {total - len(ocr_needed)}p, OCR 대상: {len(ocr_needed)}p")

        # OCR이 필요한 페이지 처리
        # ocr_pages: 실제 OCR로 처리된 페이지 idx 집합 (소스 표기용)
        ocr_pages: set[int] = set()
        if ocr_needed and ocr_on:
            use_gv = self.ocr.use_google_vision
            engine = "Google Vision" if use_gv else "Tesseract"
            prog(12, f"OCR 시작 (이미지 전용 {len(ocr_needed)}페이지 · {engine})…")
            logger.info(f"[{job_id}] OCR 엔진: {engine} | 대상: {len(ocr_needed)}p")

            ocr_done = 0

            if use_gv:
                # ── Google Vision: 렌더링 + 배치 전송 완전 병렬 ──────────
                # 1) 모든 페이지 동시 렌더링
                prog(12, f"페이지 렌더링 중… ({len(ocr_needed)}장 병렬)")
                render_tasks = [
                    loop.run_in_executor(_executor, _render_page_to_pil, pdf_path, idx, OCR_DPI)
                    for idx in ocr_needed
                ]
                rendered_imgs = await asyncio.gather(*render_tasks)
                prog(18, f"렌더링 완료 · Google Vision 동시 전송 중…")

                # 2) 배치 분할 후 모든 배치 동시 전송 (순차→병렬)
                valid_items = [(idx, img) for idx, img in zip(ocr_needed, rendered_imgs) if img is not None]
                gv_failed: list[int] = []

                batches = [
                    valid_items[s:s + GV_BATCH_SIZE]
                    for s in range(0, len(valid_items), GV_BATCH_SIZE)
                ]

                def _gv_batch(batch_items):
                    return self.ocr.gv_ocr_batch(batch_items)

                # 모든 배치 동시 실행 (GV API는 동시 요청 허용)
                batch_tasks = [
                    loop.run_in_executor(_executor, _gv_batch, batch)
                    for batch in batches
                ]
                batch_results = await asyncio.gather(*batch_tasks)

                for batch, result in zip(batches, batch_results):
                    for idx, _ in batch:
                        text = result.get(idx, "")
                        if text.strip():
                            raw_texts[idx] = text
                            ocr_pages.add(idx)
                        else:
                            gv_failed.append(idx)

                ocr_done = len(valid_items)
                prog(45, f"Google Vision OCR 완료 ({ocr_done}/{len(ocr_needed)}페이지)")

                # 3) GV 실패 페이지 Tesseract 폴백
                if gv_failed:
                    logger.warning(f"[{job_id}] GV 실패 {len(gv_failed)}p → Tesseract 폴백")
                    for idx in gv_failed:
                        text = await loop.run_in_executor(_executor, _tesseract_ocr_page, pdf_path, idx)
                        if text.strip():
                            raw_texts[idx] = text
                            ocr_pages.add(idx)
            else:
                # ── Tesseract: 3페이지씩 병렬 처리 ──────────────────────
                TESS_BATCH = 3
                for batch_start in range(0, len(ocr_needed), TESS_BATCH):
                    batch_idxs = ocr_needed[batch_start:batch_start + TESS_BATCH]
                    tess_tasks = [
                        loop.run_in_executor(_executor, _tesseract_ocr_page, pdf_path, idx)
                        for idx in batch_idxs
                    ]
                    tess_results = await asyncio.gather(*tess_tasks)
                    for idx, text in zip(batch_idxs, tess_results):
                        if text.strip():
                            raw_texts[idx] = text
                            ocr_pages.add(idx)
                    ocr_done += len(batch_idxs)
                    pct = 12 + int((ocr_done / len(ocr_needed)) * 33)  # 12~45%
                    prog(pct, f"Tesseract OCR… ({ocr_done}/{len(ocr_needed)}페이지)")
                    await asyncio.sleep(0)

            ocr_hit = len(ocr_pages)
            logger.info(f"[{job_id}] OCR 완료 ({engine}): {len(ocr_needed)}p → {ocr_hit}p 텍스트 추출 성공")
        elif ocr_needed and not ocr_on:
            logger.warning(f"[{job_id}] OCR 비활성 – {len(ocr_needed)}p 이미지 전용 페이지 텍스트 미추출")

        # ── 3. 규칙 탐지 (전 페이지 — OCR 텍스트 포함) ──────────
        prog(46, "규칙 탐지 중…")
        rule_hits_by_page: dict[str, list] = {}

        for i in range(total):
            text     = raw_texts.get(i, "")
            page_num = i + 1
            # 소스: OCR로 추출한 텍스트면 'ocr', PyMuPDF 직접 추출이면 'rule'
            text_source = "ocr" if i in ocr_pages else "rule"
            hits     = self.rules.detect(text, page_num)
            if hits:
                key = str(page_num)
                rule_hits_by_page[key] = [
                    {
                        "type":           h.detection_type.value,
                        "content":        h.detected_text or "",
                        "judgment":       h.verdict.value,
                        "reason":         h.reason,
                        "recommendation": h.recommendation,
                        "confidence":     h.confidence,
                        "source":         text_source,  # 'ocr' or 'rule'
                    }
                    for h in hits
                ]
            if i % 20 == 0:
                await asyncio.sleep(0)

        rule_total = sum(len(v) for v in rule_hits_by_page.values())
        logger.info(f"[{job_id}] 규칙 탐지 완료: {rule_total}건 (OCR 포함)")
        prog(50, f"규칙 탐지 {rule_total}건 · 이미지 변환 시작…")

        # ── 4. 페이지 이미지 변환 (Claude Vision용, 배치 병렬) ───
        page_images = []  # [{"page": int, "b64": str, "media_type": str}]

        if claude_on:
            prog(50, f"PDF 페이지 이미지 변환 중 (0/{total})…")
            RENDER_BATCH = 4
            for batch_start in range(0, total, RENDER_BATCH):
                batch_end = min(batch_start + RENDER_BATCH, total)
                tasks = [
                    loop.run_in_executor(_executor, _render_page_to_b64, svc, i, RENDER_DPI)
                    for i in range(batch_start, batch_end)
                ]
                results = await asyncio.gather(*tasks)
                for i, b64 in zip(range(batch_start, batch_end), results):
                    if b64:
                        page_images.append({
                            "page":       i + 1,
                            "b64":        b64,
                            "media_type": "image/jpeg",
                        })
                pct = 50 + int((batch_end / total) * 15)  # 50~65%
                prog(pct, f"이미지 변환 중 ({batch_end}/{total})…")
                await asyncio.sleep(0)

            prog(65, f"{len(page_images)}/{total}페이지 변환 완료 · Claude Vision 분석 시작…")
        else:
            # Claude 없으면 이미지 변환 단계 건너뜀
            prog(65, "Claude Vision 비활성 — 규칙+OCR 결과로 리포트 생성…")

        # ── 5. Claude Vision 배치 분석 (병렬, Claude 활성 시만) ──
        all_vision_items: list[dict] = []

        if claude_on and page_images:
            company_dict = load_dict()
            logo_b64     = _load_logo_b64()

            batches     = [page_images[i:i+PAGES_PER_BATCH]
                           for i in range(0, len(page_images), PAGES_PER_BATCH)]
            batch_count = len(batches)

            BATCH_CONCURRENCY = 5
            sem       = asyncio.Semaphore(BATCH_CONCURRENCY)
            completed = [0]

            async def run_batch(bi: int, batch: list):
                async with sem:
                    batch_rule_hits = {}
                    for pg in batch:
                        key = str(pg["page"])
                        if key in rule_hits_by_page:
                            batch_rule_hits[key] = rule_hits_by_page[key]
                    try:
                        items = await loop.run_in_executor(
                            _executor,
                            self.judge.judge_image_batch,
                            batch,
                            logo_b64,
                            company_dict,
                            batch_rule_hits or None,
                        )
                        logger.info(f"[{job_id}] Vision 배치 {bi+1}/{batch_count}: {len(items)}건")
                        return items
                    except Exception as e:
                        logger.error(f"[{job_id}] Vision 배치 {bi+1} 오류: {e}")
                        return []
                    finally:
                        completed[0] += 1
                        pct = 65 + int((completed[0] / batch_count) * 25)  # 65~90%
                        prog(pct, f"Claude Vision 분석 중 ({completed[0]}/{batch_count} 배치)…")

            vision_tasks   = [run_batch(bi, batch) for bi, batch in enumerate(batches)]
            vision_results = await asyncio.gather(*vision_tasks)
            for items in vision_results:
                all_vision_items.extend(items)

            logger.info(f"[{job_id}] Claude Vision 완료: {len(all_vision_items)}건")

        prog(90, "결과 합산 중…")

        # ── 6. 규칙 + Vision 합산 ──────────────────────────────
        merged = _merge_results(rule_hits_by_page, all_vision_items, total)

        # ── 7. 원본 파일 삭제 ───────────────────────────────────
        svc.close()
        try:
            _wipe_file(pdf_path)
        except Exception as e:
            logger.warning(f"[{job_id}] 파일 삭제 실패: {e}")

        # ── 8. 리포트 생성 ──────────────────────────────────────
        prog(95, "리포트 생성 중…")
        elapsed = round(time.time() - t0, 2)
        report  = _build_report(job_id, filename, svc.total_pages, merged, elapsed,
                                 claude_on=claude_on, ocr_on=ocr_on)

        prog(100, "검증 완료")
        logger.info(
            f"[{job_id}] 완료 {elapsed}s | "
            f"위반:{report['violation_count']} 주의:{report['caution_count']} | "
            f"모드: {' + '.join(mode_desc)}"
        )
        return report


# ── 결과 합산 ────────────────────────────────────────────────────
def _merge_results(rule_hits_by_page: dict, vision_items: list, total_pages: int) -> dict:
    """제대로 하면 규칙 탐지(OCR 포함) + Vision 결과를 페이지별로 합산"""
    page_map: dict[int, list] = {}

    # Vision 항목 처리
    for it in vision_items:
        try:
            p = int(it.get("page", 0))
        except (ValueError, TypeError):
            p = 1
        if p < 1:
            p = 1
        content = it.get("content", "")
        dtype   = it.get("type", "기타")

        # ── "텍스트 추출 탐지" 더미 항목 필터링 ──────────────────────
        _DUMMY_TYPES = (
            "텍스트 추출 탐지", "텍스트추출탐지", "텍스트 탐지", "텍스트탐지",
            "텍스트 탐지 실명", "텍스트탐지실명", "참여인력 실명", "텍스트 실명",
        )
        _DUMMY_CONTENTS = (
            "텍스트 추출로 확인된 위반 요소 존재",
            "텍스트 추출 위반 확인",
            "텍스트 추출로 확인된 위반",
            "텍스트 추출로 탐지된 사전 등록 실명",
            "사전 등록 실명 목록의 이름들이 텍스트 추출로 탐지됨",
            "텍스트 추출로 사전 등록 실명이 탐지됨",
        )
        _DUMMY_KEYWORDS = ("텍스트 추출로 탐지된", "사전 등록 실명 목록", "텍스트 추출로 사전 등록")
        _content_strip = content.strip()
        if (dtype in _DUMMY_TYPES
                or _content_strip in _DUMMY_CONTENTS
                or any(kw in _content_strip for kw in _DUMMY_KEYWORDS)):
            continue

        # vision끼리 대소문자 무시 중복 제거
        already = any(
            d["detected_text"].lower() == content.lower() and d["detection_type"] == dtype
            for d in page_map.get(p, [])
        )
        if already:
            continue

        # ── 일반 명사 체크
        from services.rule_detector import _COMMON_WORDS as _CW
        import re as _re
        content_stripped = content.strip()
        _NAME_DTYPES = ("참여인력명", "인력명", "업체명", "대표자명", "기타", "인물명", "이름", "성명")
        if content_stripped in _CW and (dtype in _NAME_DTYPES or "인력" in dtype or "이름" in dtype or "명" in dtype):
            page_map.setdefault(p, []).append({
                "detection_type":  dtype,
                "detected_text":   content,
                "verdict":         "허용",
                "reason":          f"맥락상 일반 명사로 판단 – 인명 오탐 제외 ('{content_stripped}'은 고유 인명이 아님)",
                "recommendation":  "검증 불필요 – 일반 명사/단어로 확인됨",
                "confidence":      0.98,
                "source":          "vision",
            })
            continue

        # 2글자 이하 인력명: reason에 기관명 키워드가 있으면 허용으로 처리 (결과에 표시)
        if len(content_stripped) <= 2 and (dtype in ("참여인력명", "인력명", "업체명", "대표자명") or "인력" in dtype or "이름" in dtype):
            reason_text = it.get("reason", "") + " " + it.get("recommendation", "")
            if _re.search(r'공단|공사|위원회|연구원|연구소|진흥원|협회|학회|재단|센터|기관|건강보험|국민연금|서민금융|대국민|안전처|안전부', reason_text):
                page_map.setdefault(p, []).append({
                    "detection_type":  dtype,
                    "detected_text":   content,
                    "verdict":         "허용",
                    "reason":          f"맥락상 기관명의 일부로 판단 – 인명 오탐 제외 (reason: {reason_text[:60]})",
                    "recommendation":  "기관명 복합어로 확인됨, 검증 불필요",
                    "confidence":      0.97,
                    "source":          "vision",
                })
                continue

        # ── 공공기관 로고 오탐 추가 차단 (claude_judge 통과 후에도 재확인) ──────
        from services.claude_judge import _is_logo_type, _PUBLIC_ORG_KEYWORDS
        if (_is_logo_type(dtype) or _is_logo_type(content)) and it.get("judgment") != "허용":
            for _kw in _PUBLIC_ORG_KEYWORDS:
                if (_kw.lower() in content.lower()
                        or _kw.lower() in it.get("reason", "").lower()):
                    it["judgment"] = "허용"
                    it["reason"] = f"공공기관/발주기관 로고 오탐 차단 ({_kw})"
                    it["recommendation"] = ""
                    break

        page_map.setdefault(p, []).append({
            "detection_type":  dtype,
            "detected_text":   content,
            "verdict":         it.get("judgment", "주의"),
            "reason":          it.get("reason", ""),
            "recommendation":  it.get("recommendation", ""),
            "confidence":      0.9,
            "source":          "vision",
        })

    # 규칙 항목 처리: Vision과 같은 텍스트면 source를 합치, 없으면 신규 추가
    for page_str, hits in rule_hits_by_page.items():
        try:
            p = int(page_str)
        except ValueError:
            continue
        for h in hits:
            content = h.get("content", "").lower()
            rule_src = h.get("source", "rule")  # 'ocr' or 'rule'
            # vision과 중복 여부 확인
            matched_idx = None
            for idx, d in enumerate(page_map.get(p, [])):
                vc = d["detected_text"].lower()
                if content and vc and (
                    content == vc or
                    (content in vc and len(content) >= 4 and len(content) / len(vc) > 0.5) or
                    (vc in content and len(vc) >= 4 and len(vc) / len(content) > 0.5)
                ):
                    matched_idx = idx
                    break
            if matched_idx is not None:
                # 중복 항목: source에 rule 추가
                existing = page_map[p][matched_idx]
                existing_src = existing.get("source", "vision")
                if existing_src == "vision":
                    existing["source"] = f"{rule_src}+vision"
                # ★ 로고 재비교로 허용 처리된 항목은 rule이 덮어쓰지 못함
                from services.claude_judge import _is_logo_type as _ilt
                if existing.get("verdict") == "허용" and _ilt(existing.get("detection_type", "")):
                    pass  # 로고 재비교 허용 결과 보존
                else:
                    # 판정은 더 강한 쪽으로
                    weight = {"위반": 2, "주의": 1, "허용": 0}
                    if weight.get(h.get("judgment", "주의"), 0) > weight.get(existing["verdict"], 0):
                        existing["verdict"] = h.get("judgment", "주의")
            else:
                # source: 'ocr'이면 OCR로 읽은 텍스트에서 탐지, 'rule'이면 PyMuPDF 텍스트에서 탐지
                page_map.setdefault(p, []).append({
                    "detection_type":  h.get("type", "기타"),
                    "detected_text":   h.get("content", ""),
                    "verdict":         h.get("judgment", "주의"),
                    "reason":          h.get("reason", ""),
                    "recommendation":  h.get("recommendation", ""),
                    "confidence":      h.get("confidence", 0.95),
                    "source":          rule_src,
                })

    return page_map


def _build_report(job_id: str, filename: str, total_pages: int,
                  page_map: dict, elapsed: float,
                  claude_on: bool = False, ocr_on: bool = True) -> dict:
    """page_map → VerificationReport dict (프론트엔드 호환)"""
    page_results = []
    for p in range(1, total_pages + 1):
        dets = page_map.get(p, [])
        vc = sum(1 for d in dets if d["verdict"] == "위반")
        cc = sum(1 for d in dets if d["verdict"] == "주의")
        ac = sum(1 for d in dets if d["verdict"] == "허용")
        page_results.append({
            "page_number":     p,
            "thumbnail_b64":   None,
            "detections":      dets,
            "violation_count": vc,
            "caution_count":   cc,
            "allowed_count":   ac,
        })

    vc_total = sum(p["violation_count"] for p in page_results)
    cc_total = sum(p["caution_count"]   for p in page_results)
    ac_total = sum(p["allowed_count"]   for p in page_results)

    if vc_total >= 5:                      risk = "HIGH"
    elif vc_total >= 1 or cc_total >= 5:   risk = "MEDIUM"
    else:                                  risk = "LOW"

    all_dets = [d for pr in page_results for d in pr["detections"]]

    def has_type(t):
        return any(t in d["detection_type"] and d["verdict"] == "위반" for d in all_dets)

    notes = []
    notes.append("업체명 직접 노출 있음"  if has_type("업체") or has_type("회사") else "명확한 업체명 노출 없음")
    notes.append("참여인력 실명 노출 있음" if has_type("인력") or has_type("대표") else "참여인력 실명 없음")
    notes.append("이메일/URL 노출 있음"   if has_type("이메일") or has_type("URL") else "이메일/URL 없음")
    if cc_total > 0:
        notes.append(f"간접 식별 가능 표현 {cc_total}건 발견")

    # 분석 모드 표기
    modes = []
    if claude_on: modes.append("Claude Vision AI")
    if ocr_on:    modes.append("OCR")
    modes.append("규칙 탐지")
    analysis_mode = " + ".join(modes)

    # 프론트엔드 대시보드용 flat items 배열
    flat_items = []
    for pr in page_results:
        p_num = pr["page_number"]
        for d in pr["detections"]:
            flat_items.append({
                "page":           p_num,
                "type":           d.get("detection_type", "기타"),
                "content":        d.get("detected_text", ""),
                "judgment":       d.get("verdict", "주의"),
                "reason":         d.get("reason", ""),
                "recommendation": d.get("recommendation", ""),
                "source":         d.get("source", "rule"),  # 'rule'|'ocr'|'vision'
            })

    return {
        "job_id":                  job_id,
        "filename":                filename,
        "total_pages":             total_pages,
        "page_count":              total_pages,
        "processing_time_seconds": elapsed,
        "elapsed_sec":             elapsed,
        "created_at":              datetime.now().isoformat(),
        "risk_level":              risk,
        "violation_count":         vc_total,
        "caution_count":           cc_total,
        "allowed_count":           ac_total,
        "_analysis_mode":          analysis_mode,
        "items":                   flat_items,
        "summary_notes":           notes,
        "page_results":            page_results,
        "summary": {
            "no_company_name": not (has_type("업체") or has_type("회사")),
            "no_personnel":    not (has_type("인력") or has_type("대표")),
            "no_email_url":    not (has_type("이메일") or has_type("URL")),
            "indirect_count":  cc_total,
            "logo_detected":   any("로고" in d["detection_type"] and d["verdict"] != "허용" for d in all_dets),
            "metadata_clean":  True,
            "notes":           notes,
        },
    }


def _load_logo_b64() -> Optional[str]:
    """저장된 로고 레퍼런스 이미지 로드"""
    logo_path = DATA_DIR / "logo_reference.png"
    if logo_path.exists():
        try:
            return base64.b64encode(logo_path.read_bytes()).decode("utf-8")
        except Exception:
            pass
    return None


# ── 싱글톤 ──────────────────────────────────────────────────────
_inst: ServerPipeline | None = None

def get_server_pipeline() -> ServerPipeline:
    global _inst
    if _inst is None:
        _inst = ServerPipeline()
    return _inst
