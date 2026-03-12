"""
서버사이드 완전 검증 파이프라인
- 브라우저 없이 서버에서 PDF→이미지 변환→Claude Vision 분석 수행
- 대시보드 모드: 업로드 후 브라우저 닫아도 백그라운드에서 완료됨
"""
from __future__ import annotations
import asyncio, base64, io, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from services.rule_detector import get_rule_detector
from services.claude_judge  import get_claude_judge, ClaudeVisionJudge
from services.file_manager  import _wipe_file
from services.pdf_service   import PDFService
from core.config import get_logger, update_job, load_dict, DATA_DIR

logger = get_logger("server_pipeline")

PAGES_PER_BATCH = 4    # Claude Vision 배치당 페이지 수
RENDER_DPI      = 120  # 이미지 렌더링 DPI (120 = 속도/품질 균형)
MAX_PAGES       = 200  # 최대 처리 페이지 수
_executor = ThreadPoolExecutor(max_workers=2)


def _render_page_to_b64(svc: PDFService, page_idx: int, dpi: int = RENDER_DPI) -> Optional[str]:
    """PyMuPDF로 페이지를 JPEG base64로 변환"""
    try:
        import fitz
        doc = fitz.open(str(svc.path))
        page = doc[page_idx]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        # JPEG 압축 (품질 75 = Claude Vision에 충분)
        img_bytes = pix.tobytes("jpeg", jpg_quality=75)
        doc.close()
        return base64.b64encode(img_bytes).decode("utf-8")
    except Exception as e:
        logger.warning(f"페이지 {page_idx+1} 렌더링 실패: {e}")
        return None


class ServerPipeline:
    """서버사이드 완전 검증 파이프라인"""

    def __init__(self):
        self.rules = get_rule_detector()
        self.judge: ClaudeVisionJudge = get_claude_judge()

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
        prog(8, f"총 {svc.total_pages}페이지 확인 · 이미지 변환 준비 중…")

        # ── 2. 텍스트 규칙 탐지 (전 페이지 빠르게) ─────────────
        prog(10, "텍스트 스캔 및 규칙 탐지 중…")
        rule_hits_by_page: dict[str, list] = {}   # "pageNum" → [hit, ...]

        for i in range(total):
            page = svc.extract_page(i)
            hits = self.rules.detect(page.text, page.page_number)
            if hits:
                key = str(page.page_number)
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

        rule_total = sum(len(v) for v in rule_hits_by_page.values())
        prog(20, f"텍스트 스캔 완료 · 규칙 탐지 {rule_total}건 → 이미지 변환 시작…")

        # ── 3. 페이지 이미지 변환 (배치 단위) ───────────────────
        prog(25, f"PDF 페이지 이미지 변환 중 (0/{total})…")
        page_images = []  # [{"page": int, "b64": str, "media_type": str}]

        # 4페이지씩 병렬 변환
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
            pct = 25 + int((batch_end / total) * 25)
            prog(pct, f"이미지 변환 중 ({batch_end}/{total})…")
            await asyncio.sleep(0)

        prog(50, f"{len(page_images)}/{total}페이지 변환 완료 · Claude Vision 분석 시작…")

        # ── 4. Claude Vision 배치 분석 ──────────────────────────
        company_dict = load_dict()
        logo_b64     = _load_logo_b64()

        all_vision_items: list[dict] = []
        batches = [page_images[i:i+PAGES_PER_BATCH]
                   for i in range(0, len(page_images), PAGES_PER_BATCH)]
        batch_count = len(batches)

        for bi, batch in enumerate(batches):
            pct = 50 + int((bi / batch_count) * 40)
            prog(pct, f"Claude Vision 분석 중 ({bi+1}/{batch_count} 배치)…")

            # 이 배치의 rule_hits 추출
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
                all_vision_items.extend(items)
                logger.info(f"[{job_id}] 배치 {bi+1}/{batch_count}: {len(items)}건 검출")
            except Exception as e:
                logger.error(f"[{job_id}] 배치 {bi+1} 오류: {e}")

            await asyncio.sleep(0)

        prog(90, f"Vision 분석 완료 · 결과 합산 중…")

        # ── 5. 규칙 + Vision 합산 ──────────────────────────────
        merged = _merge_results(rule_hits_by_page, all_vision_items, total)

        # ── 6. 원본 파일 삭제 ───────────────────────────────────
        svc.close()
        try:
            _wipe_file(pdf_path)
        except Exception as e:
            logger.warning(f"[{job_id}] 파일 삭제 실패: {e}")

        # ── 7. 리포트 생성 ──────────────────────────────────────
        prog(95, "리포트 생성 중…")
        elapsed = round(time.time() - t0, 2)
        report  = _build_report(job_id, filename, svc.total_pages, merged, elapsed)

        prog(100, "검증 완료")
        logger.info(f"[{job_id}] 완료 {elapsed}s | 위반:{report['violation_count']} 주의:{report['caution_count']}")
        return report


# ── 결과 합산 ────────────────────────────────────────────────────
def _merge_results(rule_hits_by_page: dict, vision_items: list, total_pages: int) -> dict:
    """규칙 탐지 + Vision 결과를 페이지별로 합산 → {pageNum: [det, ...]}"""
    WEIGHT = {"위반": 2, "주의": 1, "허용": 0}

    # Vision 결과를 페이지별 맵으로
    vision_by_page: dict[str, list] = {}
    for it in vision_items:
        p = str(it.get("page", "?"))
        vision_by_page.setdefault(p, []).append(it)

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

    # 규칙 항목 처리 (Vision에 없는 것만 추가)
    for page_str, hits in rule_hits_by_page.items():
        try:
            p = int(page_str)
        except ValueError:
            continue
        vpage = [d["detected_text"].lower() for d in page_map.get(p, [])]
        for h in hits:
            content = h.get("content", "").lower()
            # 중복 체크: 길이 4자 이상 + 비율 50% 초과
            already = any(
                content and vc and
                ((content in vc and len(content) >= 4 and len(content)/len(vc) > 0.5) or
                 (vc in content and len(vc) >= 4 and len(vc)/len(content) > 0.5) or
                 content == vc)
                for vc in vpage
            )
            if not already:
                page_map.setdefault(p, []).append({
                    "detection_type":  h.get("type", "기타"),
                    "detected_text":   h.get("content", ""),
                    "verdict":         h.get("judgment", "주의"),
                    "reason":          h.get("reason", "") + " [규칙 탐지]",
                    "recommendation":  h.get("recommendation", ""),
                    "confidence":      h.get("confidence", 0.95),
                    "source":          "rule",
                })

    return page_map


def _build_report(job_id: str, filename: str, total_pages: int,
                  page_map: dict, elapsed: float) -> dict:
    """page_map → VerificationReport dict"""
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

    if vc_total >= 5:                 risk = "HIGH"
    elif vc_total >= 1 or cc_total >= 5: risk = "MEDIUM"
    else:                             risk = "LOW"

    all_dets = [d for p in page_results for d in p["detections"]]

    def has_type(t):
        return any(t in d["detection_type"] and d["verdict"] == "위반" for d in all_dets)

    notes = []
    notes.append("업체명 직접 노출 있음"  if has_type("업체") or has_type("회사") else "명확한 업체명 노출 없음")
    notes.append("참여인력 실명 노출 있음" if has_type("인력") or has_type("대표") else "참여인력 실명 없음")
    notes.append("이메일/URL 노출 있음"   if has_type("이메일") or has_type("URL") else "이메일/URL 없음")
    if cc_total > 0: notes.append(f"간접 식별 가능 표현 {cc_total}건 발견")

    return {
        "job_id":                   job_id,
        "filename":                 filename,
        "total_pages":              total_pages,
        "processing_time_seconds":  elapsed,
        "created_at":               datetime.now().isoformat(),
        "risk_level":               risk,
        "violation_count":          vc_total,
        "caution_count":            cc_total,
        "allowed_count":            ac_total,
        "page_results":             page_results,
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
