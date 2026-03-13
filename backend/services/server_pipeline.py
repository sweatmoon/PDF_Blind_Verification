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

from services.rule_detector import get_rule_detector
from services.claude_judge  import get_claude_judge, ClaudeVisionJudge
from services.ocr_service   import get_ocr
from services.file_manager  import _wipe_file
from services.pdf_service   import PDFService
from core.config import get_logger, update_job, load_dict, DATA_DIR

logger = get_logger("server_pipeline")

PAGES_PER_BATCH   = 4    # Claude Vision 배치당 페이지 수
RENDER_DPI        = 120  # Claude Vision용 이미지 DPI
OCR_DPI           = 150  # OCR용 이미지 DPI (150 = 속도/품질 균형, 200은 너무 느림)
MAX_PAGES         = 200  # 최대 처리 페이지 수
OCR_TEXT_THRESHOLD = 50  # 이 글자 수 미만이면 OCR 실행
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


# ── OCR 실행 (단일 페이지, ThreadPool에서 호출) ───────────────────
def _ocr_page(svc: PDFService, page_idx: int) -> str:
    """페이지를 OCR_DPI로 렌더링 후 Tesseract OCR 실행 → 텍스트 반환"""
    try:
        ocr = get_ocr()
        if not ocr.enabled:
            return ""
        img = svc.render_for_ocr(page_idx, dpi=OCR_DPI)
        if img is None:
            return ""
        return ocr.from_image(img)
    except Exception as e:
        logger.warning(f"OCR 페이지 {page_idx+1} 실패: {e}")
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
        claude_on = self.judge.client is not None
        ocr_on    = self.ocr.enabled
        mode_desc = []
        if claude_on: mode_desc.append("Claude Vision")
        if ocr_on:    mode_desc.append("OCR")
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

        # OCR이 필요한 페이지 병렬 처리
        if ocr_needed and ocr_on:
            prog(12, f"OCR 실행 중 (대상 {len(ocr_needed)}페이지)…")
            # 메모리 안전을 위해 3페이지씩 병렬 처리 (Tesseract 다중 인스턴스 메모리 절약)
            OCR_BATCH = 3
            ocr_done  = 0
            for batch_start in range(0, len(ocr_needed), OCR_BATCH):
                batch_idxs = ocr_needed[batch_start:batch_start + OCR_BATCH]
                ocr_tasks  = [
                    loop.run_in_executor(_executor, _ocr_page, svc, idx)
                    for idx in batch_idxs
                ]
                ocr_results = await asyncio.gather(*ocr_tasks)
                for idx, ocr_text in zip(batch_idxs, ocr_results):
                    if ocr_text.strip():
                        raw_texts[idx] = ocr_text   # OCR 결과로 교체
                ocr_done += len(batch_idxs)
                pct = 12 + int((ocr_done / len(ocr_needed)) * 8)  # 12~20%
                prog(pct, f"OCR 진행 중… ({ocr_done}/{len(ocr_needed)})")
                await asyncio.sleep(0)

            ocr_hit = sum(1 for i in ocr_needed if len(raw_texts[i].strip()) >= OCR_TEXT_THRESHOLD)
            logger.info(f"[{job_id}] OCR 완료: {len(ocr_needed)}p 처리 → {ocr_hit}p 텍스트 추출 성공")
        elif ocr_needed and not ocr_on:
            logger.warning(f"[{job_id}] OCR 비활성 – {len(ocr_needed)}p 이미지 전용 페이지 텍스트 미추출")

        # ── 3. 규칙 탐지 (전 페이지 — OCR 텍스트 포함) ──────────
        prog(20, "규칙 탐지 중…")
        rule_hits_by_page: dict[str, list] = {}

        for i in range(total):
            text     = raw_texts.get(i, "")
            page_num = i + 1
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
                    }
                    for h in hits
                ]
            if i % 20 == 0:
                await asyncio.sleep(0)

        rule_total = sum(len(v) for v in rule_hits_by_page.items())
        rule_total = sum(len(v) for v in rule_hits_by_page.values())
        logger.info(f"[{job_id}] 규칙 탐지 완료: {rule_total}건 (OCR 포함)")
        prog(25, f"규칙 탐지 {rule_total}건 → 이미지 변환 시작…")

        # ── 4. 페이지 이미지 변환 (Claude Vision용, 배치 병렬) ───
        page_images = []  # [{"page": int, "b64": str, "media_type": str}]

        if claude_on:
            prog(25, f"PDF 페이지 이미지 변환 중 (0/{total})…")
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
                pct = 25 + int((batch_end / total) * 25)  # 25~50%
                prog(pct, f"이미지 변환 중 ({batch_end}/{total})…")
                await asyncio.sleep(0)

            prog(50, f"{len(page_images)}/{total}페이지 변환 완료 · Claude Vision 분석 시작…")
        else:
            # Claude 없으면 이미지 변환 단계 건너뜀
            prog(50, "Claude Vision 비활성 — 규칙+OCR 결과로 리포트 생성…")

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
                        pct = 50 + int((completed[0] / batch_count) * 40)  # 50~90%
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
    """규칙 탐지(OCR 포함) + Vision 결과를 페이지별로 합산"""
    page_map: dict[int, list] = {}

    # Vision 항목 처리
    for it in vision_items:
        try:
            p = int(it.get("page", 0))
        except (ValueError, TypeError):
            p = 1
        if p < 1:
            p = 1
        page_map.setdefault(p, []).append({
            "detection_type":  it.get("type", "기타"),
            "detected_text":   it.get("content", ""),
            "verdict":         it.get("judgment", "주의"),
            "reason":          it.get("reason", ""),
            "recommendation":  it.get("recommendation", ""),
            "confidence":      0.9,
            "source":          "vision",
        })

    # 규칙 항목 처리 (Vision과 중복되지 않는 것만 추가)
    for page_str, hits in rule_hits_by_page.items():
        try:
            p = int(page_str)
        except ValueError:
            continue
        vpage = [d["detected_text"].lower() for d in page_map.get(p, [])]
        for h in hits:
            content = h.get("content", "").lower()
            already = any(
                content and vc and
                ((content in vc and len(content) >= 4 and len(content) / len(vc) > 0.5) or
                 (vc in content and len(vc) >= 4 and len(vc) / len(content) > 0.5) or
                 content == vc)
                for vc in vpage
            )
            if not already:
                page_map.setdefault(p, []).append({
                    "detection_type":  h.get("type", "기타"),
                    "detected_text":   h.get("content", ""),
                    "verdict":         h.get("judgment", "주의"),
                    "reason":          h.get("reason", "") + " [규칙+OCR 탐지]",
                    "recommendation":  h.get("recommendation", ""),
                    "confidence":      h.get("confidence", 0.95),
                    "source":          "rule",
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
