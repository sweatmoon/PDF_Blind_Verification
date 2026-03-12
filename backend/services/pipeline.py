"""
검증 파이프라인 – 전체 흐름 조율

1. PDF 파싱 (텍스트·이미지·메타데이터)
2. 규칙 기반 탐지
3. OCR 탐지 (이미지 내 텍스트)
4. Claude 의미 판정
5. 보고서 생성
6. 원본·중간파일 즉시 삭제
"""
from __future__ import annotations
import asyncio, time
from datetime import datetime
from pathlib import Path
from typing import List

from models.schemas import (
    DetectionResult, DetectionType, VerdictType, RiskLevel,
    PageResult, DocumentSummary, VerificationReport,
)
from services.pdf_service   import PDFService
from services.ocr_service   import get_ocr
from services.rule_detector import get_rule_detector
from services.claude_judge  import get_claude_judge
from services.file_manager  import _wipe_file
from core.config import get_logger, update_job

logger = get_logger("pipeline")


class Pipeline:

    def __init__(self):
        self.ocr   = get_ocr()
        self.rules = get_rule_detector()
        self.judge = get_claude_judge()

    # ═══════════════════════════════════════════════════════
    async def run(self, job_id: str, pdf_path: Path,
                  filename: str) -> VerificationReport:
        t0 = time.time()
        logger.info(f"[{job_id}] 파이프라인 시작: {filename}")

        def prog(pct: int, msg: str):
            update_job(job_id, progress=pct, message=msg)

        # ── 1. PDF 열기 ───────────────────────────────────
        prog(5, "PDF 파싱 중…")
        svc = PDFService(pdf_path)
        if not svc.open():
            raise RuntimeError("PDF 파일을 열 수 없습니다.")

        total = svc.total_pages
        prog(10, f"총 {total}페이지 감지")

        # ── 2. 메타데이터 분석 ────────────────────────────
        prog(12, "메타데이터 분석…")
        meta_results = self.judge.judge_metadata(
            svc.metadata, self.rules._is_allowed)
        meta_page = self._make_page_result(0, meta_results, None)

        page_results: List[PageResult] = [meta_page] if meta_results else []

        # ── 3. 페이지별 분석 ─────────────────────────────
        for i in range(total):
            pct = 12 + int(i / total * 70)
            prog(pct, f"페이지 {i+1}/{total} 분석…")
            pr = await self._analyze_page(svc, i)
            page_results.append(pr)
            if i % 3 == 0:
                await asyncio.sleep(0)   # 이벤트루프 양보

        svc.close()

        # ── 4. 원본 파일 즉시 삭제 ───────────────────────
        prog(85, "원본 파일 삭제…")
        try:
            _wipe_file(pdf_path)
            logger.info(f"[{job_id}] 원본 삭제 완료")
        except Exception as e:
            logger.error(f"[{job_id}] 원본 삭제 실패: {e}")

        # ── 5. 보고서 생성 ────────────────────────────────
        prog(95, "보고서 생성…")
        report = self._build_report(
            job_id, filename, total, page_results,
            round(time.time() - t0, 2))

        prog(100, "완료")
        logger.info(f"[{job_id}] 완료 {report.processing_time_seconds}s | "
                    f"위반:{report.violation_count} 주의:{report.caution_count}")
        return report

    # ═══════════════════════════════════════════════════════
    async def _analyze_page(self, svc: PDFService, idx: int) -> PageResult:
        page = svc.extract_page(idx)
        pnum = page.page_number
        all_det: List[DetectionResult] = []
        seen_texts: set[str] = set()

        def add_unique(items: List[DetectionResult]):
            for r in items:
                k = r.detected_text.strip()[:80].lower()
                if k and k not in seen_texts:
                    seen_texts.add(k)
                    all_det.append(r)
                elif not k:          # 이미지 설명 항목
                    all_det.append(r)

        # ── 규칙 기반 탐지 ────────────────────────────────
        rule_hits = self.rules.detect(page.text, pnum)
        add_unique(rule_hits)

        # ── OCR: 페이지 이미지 내 텍스트 ─────────────────
        if self.ocr.enabled:
            # 내장 이미지 OCR
            for img_d in page.images[:8]:
                bts = img_d.get("data", b"")
                w, h = img_d.get("w", 0), img_d.get("h", 0)
                if not bts or w < 40 or h < 40:
                    continue
                ocr_txt = self.ocr.from_bytes(bts)
                if ocr_txt and len(ocr_txt.strip()) > 5:
                    ocr_hits = self.rules.detect(ocr_txt, pnum)
                    for r in ocr_hits:
                        r.source = "ocr"
                        r.image_description = f"이미지 내 텍스트 ({w}×{h}px)"
                    add_unique(ocr_hits)

            # 스캔 페이지 전체 OCR
            if svc.is_scanned:
                pil = svc.render_for_ocr(idx, dpi=180)
                if pil:
                    full_ocr = self.ocr.from_image(pil)
                    if full_ocr:
                        full_hits = self.rules.detect(full_ocr, pnum)
                        for r in full_hits:
                            r.source = "ocr"
                        add_unique(full_hits)

        # ── 로고 추정 (이미지 크기/비율 기반) ─────────────
        logo_hits = self._logo_heuristic(page.images, pnum)
        all_det.extend(logo_hits)

        # ── Claude 판정 ───────────────────────────────────
        # A) 규칙·OCR 히트 항목 재심사 (confidence < 0.9)
        to_judge = [
            {"idx": i, "text": d.detected_text or d.image_description or "",
             "page": pnum, "type": d.detection_type.value, "source": d.source}
            for i, d in enumerate(all_det)
            if d.confidence < 0.9 and d.source != "claude"
        ]
        if to_judge:
            claude_results = self.judge.judge_page_batch(to_judge)
            self._apply_claude(all_det, claude_results)

        # B) 페이지 전체 텍스트 컨텍스트 판정 (새 위반 탐지)
        if self.judge.enabled and page.text.strip():
            meta_str = ""   # 메타데이터는 별도 처리
            ctx_hits = self.judge.judge_full_context(
                page.text, pnum, ocr_text="", metadata_str=meta_str)
            self._merge_ctx_hits(all_det, ctx_hits, pnum, seen_texts)

        thumb = svc.thumbnail_b64(idx)
        return self._make_page_result(pnum, all_det, thumb)

    # ═══════════════════════════════════════════════════════
    def _logo_heuristic(self, images: list, pnum: int) -> List[DetectionResult]:
        hits = []
        for img in images:
            w, h = img.get("w", 0), img.get("h", 0)
            if w < 30 or h < 30: continue
            ratio = w / h if h else 1
            # 로고 추정: 작고 가로 긴 이미지
            if w < 500 and h < 250 and 1.4 < ratio < 12:
                hits.append(DetectionResult(
                    page_number=pnum, detection_type=DetectionType.LOGO,
                    detected_text="", image_description=f"로고 추정 이미지 ({w}×{h}px)",
                    verdict=VerdictType.CAUTION,
                    reason="이미지 크기·비율이 회사 로고와 유사 – 직접 확인 필요",
                    recommendation="발주기관 로고: 허용 / 제안사 로고: 즉시 삭제",
                    confidence=0.55, source="image"))
        return hits

    def _apply_claude(self, detections: List[DetectionResult], claude_results: list):
        """Claude 결과로 기존 탐지 항목 업데이트"""
        verdict_map = {"위반": VerdictType.VIOLATION,
                       "주의": VerdictType.CAUTION,
                       "허용": VerdictType.ALLOWED}
        dtype_map = {v.value: v for v in DetectionType}

        for cr in claude_results:
            idx = cr.get("idx")
            if idx is None or idx >= len(detections):
                continue
            d = detections[idx]
            v = verdict_map.get(cr.get("verdict", ""), d.verdict)
            dt = dtype_map.get(cr.get("detection_type", ""), d.detection_type)
            d.verdict         = v
            d.detection_type  = dt
            d.reason          = cr.get("reason", d.reason)
            d.recommendation  = cr.get("recommendation", d.recommendation)
            d.confidence      = float(cr.get("confidence", d.confidence))
            d.source          = "claude"

    def _merge_ctx_hits(self, detections: List[DetectionResult],
                        ctx_hits: list, pnum: int, seen: set):
        """컨텍스트 Claude 결과 중 신규 항목만 추가"""
        verdict_map = {"위반": VerdictType.VIOLATION,
                       "주의": VerdictType.CAUTION,
                       "허용": VerdictType.ALLOWED}
        dtype_map = {v.value: v for v in DetectionType}

        for cr in ctx_hits:
            raw_text = cr.get("detected_text", "") or cr.get("text", "")
            k = raw_text.strip()[:80].lower()
            if k and k in seen:
                continue          # 이미 탐지된 항목
            if k:
                seen.add(k)
            verdict = verdict_map.get(cr.get("verdict", "주의"), VerdictType.CAUTION)
            if verdict == VerdictType.ALLOWED:
                continue          # 허용은 별도 추가 안 함

            detections.append(DetectionResult(
                page_number=pnum,
                detection_type=dtype_map.get(
                    cr.get("detection_type", "기타"), DetectionType.UNKNOWN),
                detected_text=raw_text,
                verdict=verdict,
                reason=cr.get("reason", "Claude 컨텍스트 분석"),
                recommendation=cr.get("recommendation", "해당 항목 확인 후 수정"),
                confidence=float(cr.get("confidence", 0.75)),
                source="claude",
            ))

    # ═══════════════════════════════════════════════════════
    @staticmethod
    def _make_page_result(pnum: int,
                           detections: List[DetectionResult],
                           thumb: str | None) -> PageResult:
        pr = PageResult(
            page_number=pnum,
            thumbnail_b64=thumb,
            detections=detections,
        )
        pr.recalc()
        return pr

    # ── 보고서 빌드 ──────────────────────────────────────
    def _build_report(self, job_id: str, filename: str, total: int,
                       page_results: List[PageResult],
                       elapsed: float) -> VerificationReport:
        v = sum(p.violation_count for p in page_results)
        c = sum(p.caution_count   for p in page_results)
        a = sum(p.allowed_count   for p in page_results)

        if v >= 5:              risk = RiskLevel.HIGH
        elif v >= 1 or c >= 5:  risk = RiskLevel.MEDIUM
        else:                   risk = RiskLevel.LOW

        all_det = [d for p in page_results for d in p.detections]

        def has(dtype, verdict):
            return any(d.detection_type == dtype and d.verdict == verdict
                       for d in all_det)

        notes = []
        company_viol = has(DetectionType.COMPANY_NAME,  VerdictType.VIOLATION)
        person_viol  = has(DetectionType.PERSONNEL,     VerdictType.VIOLATION)
        email_viol   = any(d.detection_type in (DetectionType.EMAIL, DetectionType.URL)
                           and d.verdict == VerdictType.VIOLATION for d in all_det)
        logo_det     = any(d.detection_type == DetectionType.LOGO
                           and d.verdict != VerdictType.ALLOWED for d in all_det)
        meta_viol    = has(DetectionType.METADATA, VerdictType.VIOLATION)

        notes.append("업체명 직접 노출 있음"   if company_viol else "명확한 업체명 노출 없음")
        notes.append("참여인력 실명 노출 있음"  if person_viol  else "참여인력 실명 없음")
        notes.append("이메일/URL 노출 있음"     if email_viol   else "이메일/URL 없음")
        if c > 0:
            notes.append(f"간접 식별 가능 표현 {c}건 발견")
        if logo_det:
            notes.append("로고/브랜드 이미지 감지")
        if meta_viol:
            notes.append("PDF 메타데이터에 식별정보 포함")

        summary = DocumentSummary(
            no_company_name=not company_viol,
            no_personnel=not person_viol,
            no_email_url=not email_viol,
            indirect_count=c,
            logo_detected=logo_det,
            metadata_clean=not meta_viol,
            notes=notes,
        )

        return VerificationReport(
            job_id=job_id, filename=filename,
            total_pages=total, risk_level=risk,
            violation_count=v, caution_count=c, allowed_count=a,
            page_results=page_results, summary=summary,
            created_at=datetime.now(),
            processing_time_seconds=elapsed,
        )
