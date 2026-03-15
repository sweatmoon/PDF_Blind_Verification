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
from core.config import now_kst_iso
from pathlib import Path
from typing import List, Optional

from services.rule_detector import get_rule_detector, _is_org_context
from services.claude_judge  import get_claude_judge, ClaudeVisionJudge
from services.ocr_service   import get_ocr
from services.file_manager  import _wipe_file
from services.pdf_service   import PDFService
from core.config import get_logger, update_job, load_dict, DATA_DIR
import core.analysis_log as alog

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

        # 분석 로그: job_id 활성화
        alog.set_job(job_id)
        alog.log("pipeline", "start", {"filename": filename, "job_id": job_id, "pipeline": "server"})

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
        # 규칙 탐지 상세 로그
        for _pg, _hits in rule_hits_by_page.items():
            for _h in _hits:
                logger.info(
                    f"[{job_id}] rule_hit | page={_pg} "
                    f"type={_h.get('type','?')} "
                    f"judgment={_h.get('judgment','?')} "
                    f"content={str(_h.get('content',''))[:40]!r}"
                )

        # ── OCR 품질 로그 (요구사항 7번) ────────────────────────────
        for i in range(total):
            pymupdf_len = len(raw_texts.get(i, "").strip())
            _ocr_used   = i in ocr_pages
            _ocr_engine = ("GoogleVision" if gv_on else "Tesseract") if _ocr_used else "none"
            _ocr_len    = len(raw_texts.get(i, "").strip()) if _ocr_used else 0
            _rule_cnt   = len(rule_hits_by_page.get(str(i + 1), []))
            logger.debug(
                f"[{job_id}] page={i+1} | "
                f"pymupdf_text={pymupdf_len} | "
                f"ocr_used={_ocr_used} | "
                f"ocr_engine={_ocr_engine} | "
                f"ocr_len={_ocr_len} | "
                f"rule_hits={_rule_cnt} | "
                f"vision_items=pending"   # vision은 아직 미실행
            )

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
            company_dict    = load_dict()
            logo_b64        = _load_logo_b64()
            logo_symbol_b64 = _load_logo_symbol_b64()

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
                        import functools as _ft
                        _fn = _ft.partial(
                            self.judge.judge_image_batch,
                            batch,
                            logo_b64,
                            company_dict,
                            batch_rule_hits or None,
                            logo_symbol_b64,
                        )
                        items = await loop.run_in_executor(_executor, _fn)
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

        # ── Vision 완료 후 페이지별 vision_items 카운트 보완 로그 ──────
        if all_vision_items:
            _vision_per_page: dict[int, int] = {}
            for _vi in all_vision_items:
                try:
                    _vp = int(_vi.get("page", 0))
                except Exception:
                    _vp = 0
                _vision_per_page[_vp] = _vision_per_page.get(_vp, 0) + 1
            for _vp, _vc in sorted(_vision_per_page.items()):
                logger.debug(
                    f"[{job_id}] page={_vp} | vision_items={_vc}"
                )

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

        # 분석 로그: 파이프라인 종료
        alog.log("pipeline", "finish", {
            "elapsed_sec":     elapsed,
            "total_pages":     svc.total_pages,
            "violation_count": report.get("violation_count", 0),
            "caution_count":   report.get("caution_count", 0),
            "allowed_count":   report.get("allowed_count", 0),
            "risk_level":      report.get("risk_level"),
            "mode":            mode_desc,
        })
        alog.close_job(job_id)

        return report


# ══════════════════════════════════════════════════════════════════
# _merge_results 분리 구조 (요구사항 5번)
#
#  단계 순서:
#   1. normalize_vision_items()      — 더미·중복 제거, 기본 정규화
#   2. apply_text_fp_filters()       — 일반명사/기관명 예외 (비로고 전용)
#   3. apply_logo_filters()          — 로고 공공기관 차단 (로고 전용)
#   4. apply_face_filters()          — 실루엣/아이콘 허용 (얼굴 전용)
#   5. merge_rule_and_vision()       — rule_hits 병합 + 우선순위 verdict
#   6. finalize_page_map()           — 최종 page_map 반환
#
#  우선순위 (요구사항 6번):
#   1순위: 사전 등록 실명 rule
#   2순위: 이메일/URL rule
#   3순위: 로고 후처리 (rule verdict 변경 금지)
#   4순위: Claude Vision 판정
#   5순위: OCR 텍스트 rule
# ══════════════════════════════════════════════════════════════════

# ── 더미 타입/컨텐츠 상수 ─────────────────────────────────────────
_DUMMY_TYPES: tuple = (
    "텍스트 추출 탐지", "텍스트추출탐지", "텍스트 탐지", "텍스트탐지",
    "텍스트 탐지 실명", "텍스트탐지실명", "참여인력 실명", "텍스트 실명",
)
_DUMMY_CONTENTS: tuple = (
    "텍스트 추출로 확인된 위반 요소 존재",
    "텍스트 추출 위반 확인",
    "텍스트 추출로 확인된 위반",
    "텍스트 추출로 탐지된 사전 등록 실명",
    "사전 등록 실명 목록의 이름들이 텍스트 추출로 탐지됨",
    "텍스트 추출로 사전 등록 실명이 탐지됨",
)
_DUMMY_KEYWORDS: tuple = (
    "텍스트 추출로 탐지된", "사전 등록 실명 목록", "텍스트 추출로 사전 등록"
)

# 얼굴/인물 타입 키워드
# ★ "person_candidate"는 새 아키텍처에서 Claude가 반환하는 후보 타입
_FACE_DTYPES_KW: tuple = (
    "인물", "사진", "얼굴", "face", "photo", "인물사진", "사람",
    "person_candidate",  # 새 아키텍처: Claude가 반환하는 후보 타입
)

# ★ 그래픽/아이콘 확정 키워드 (하나라도 있으면 무조건 허용)
_ICON_ALLOW_KW: tuple = (
    "실루엣", "silhouette",
    "아이콘", "icon",
    "픽토그램", "pictogram",
    "벡터", "vector",
    "일러스트", "illust",
    "캐릭터", "character",
    "다이어그램", "diagram",
    "단색", "monochrome",
    "스케치", "sketch",
    "그래픽", "graphic",
    "이모지", "emoji",
    "아이콘 형태", "icon style",
    "사람 아이콘", "person icon",
    "인물 아이콘",
    "연구자 아이콘", "직원 아이콘", "사용자 아이콘",
    "얼굴 없음", "눈코입 없음",
)

# ★ 실제 사진 확정 키워드 (이것이 명시될 때만 위반 유지)
_REAL_PHOTO_KW_SP: tuple = (
    "피부색", "피부 질감", "skin",
    "이목구비",
    "눈코입", "눈·코·입",
    "사진 질감", "photo texture",
    "실사", "real photo",
    "카메라", "촬영",
    "프로필 사진", "얼굴 사진",
)

# 공공기관 로고 허용 키워드 (하드코딩 보조)
_PUBLIC_ORG_KW_EXTRA: tuple = (
    "공단", "공사", "위원회", "연구원", "연구소", "진흥원", "협회", "학회",
    "재단", "센터", "국토교통부", "행정안전부", "보건복지부", "국가철도공단",
)


# ─────────────────────────────────────────────────────────────────
# 1단계: Vision 결과 정규화 (더미 제거 + 정수 페이지 + 중복 제거)
# ─────────────────────────────────────────────────────────────────
def normalize_vision_items(vision_items: list) -> list:
    """
    - 더미 타입/컨텐츠 항목 제거
    - page → int 변환
    - 같은 페이지 내 동일 (type, content) 중복 제거
    반환: 정규화된 item 리스트 (각 item에 _page_int 키 추가)
    """
    seen: set = set()
    out = []
    for it in vision_items:
        try:
            p = int(it.get("page", 0))
        except (ValueError, TypeError):
            p = 1
        # p < 1 강제 제거: firstSlideNum=0인 PPTX는 page=0이 정상이므로
        # 음수(-1 마스터/레이아웃 가상 페이지 등)만 1로 보정
        if p < 0:
            p = 0

        content = (it.get("content") or "").strip()
        dtype   = (it.get("type")    or "기타").strip()

        # 더미 필터
        if dtype in _DUMMY_TYPES:
            continue
        if content in _DUMMY_CONTENTS:
            continue
        if any(kw in content for kw in _DUMMY_KEYWORDS):
            continue

        # 중복 제거: person_candidate는 bbox 기반으로 구분 (같은 페이지 여러 얼굴 허용)
        # 그 외 타입은 기존대로 (페이지 + 타입 + 컨텐츠) 기준
        if "person_candidate" in dtype:
            # bbox를 key에 포함해 얼굴별로 고유 구분
            _bbox = it.get("bbox")
            _bbox_key = tuple(round(v, 3) for v in _bbox) if _bbox and len(_bbox) >= 4 else id(it)
            key = (p, dtype, _bbox_key)
        else:
            key = (p, dtype, content.lower())
        if key in seen:
            continue
        seen.add(key)

        it = dict(it)          # 원본 변형 방지
        it["_page_int"] = p
        out.append(it)

    alog.log("server_pipeline", "normalize_done", {
        "input_count":  len(vision_items),
        "output_count": len(out),
        "filtered_out": len(vision_items) - len(out),
    })
    return out


# ─────────────────────────────────────────────────────────────────
# 2단계: 텍스트 오탐 필터 (비로고 타입 전용)
# ─────────────────────────────────────────────────────────────────
def apply_text_fp_filters(items: list) -> list:
    """
    로고 타입이 아닌 항목에만 적용:
    - 일반 명사(_COMMON_WORDS) → 허용
    - 2글자 이하 + reason에 기관명 키워드 → 허용
    로고 타입 항목은 이 함수를 완전히 건너뜀 (요구사항 2번)
    """
    from services.claude_judge import _is_logo_type
    from services.rule_detector import _COMMON_WORDS as _CW
    import re as _re

    _NAME_DTYPES = frozenset((
        "참여인력명", "인력명", "업체명", "대표자명", "기타", "인물명", "이름", "성명"
    ))
    _ORG_RE = _re.compile(
        r'공단|공사|위원회|연구원|연구소|진흥원|협회|학회|재단|센터|기관|'
        r'건강보험|국민연금|서민금융|대국민|안전처|안전부'
    )

    out = []
    for it in items:
        dtype   = it.get("type", "기타")
        content = it.get("content", "")
        cs      = content.strip()

        # 로고 타입 → 텍스트 예외 규칙 완전 스킵
        if _is_logo_type(dtype) or _is_logo_type(cs):
            out.append(it)
            continue

        is_name_dtype = (
            dtype in _NAME_DTYPES
            or "인력" in dtype or "이름" in dtype or "명" in dtype
        )

        # ① 일반 명사 체크
        if cs in _CW and is_name_dtype:
            it = dict(it)
            it["judgment"] = "허용"
            it["reason"]   = (
                f"맥락상 일반 명사로 판단 – 인명 오탐 제외 "
                f"('{cs}'은 고유 인명이 아님)"
            )
            it["recommendation"] = "검증 불필요 – 일반 명사/단어로 확인됨"
            it["_fp_filtered"]   = "common_word"
            out.append(it)
            continue

        # ② 2글자 이하 + 기관명 맥락
        if (len(cs) <= 2
                and dtype in ("참여인력명", "인력명", "업체명", "대표자명")
                or (len(cs) <= 2 and ("인력" in dtype or "이름" in dtype))):
            reason_text = (it.get("reason") or "") + " " + (it.get("recommendation") or "")
            if _ORG_RE.search(reason_text):
                it = dict(it)
                it["judgment"] = "허용"
                it["reason"]   = (
                    f"맥락상 기관명의 일부로 판단 – 인명 오탐 제외 "
                    f"(reason: {reason_text[:60]})"
                )
                it["recommendation"] = "기관명 복합어로 확인됨, 검증 불필요"
                it["_fp_filtered"]   = "org_compound"
                out.append(it)
                continue

        out.append(it)
    return out


# ─────────────────────────────────────────────────────────────────
# 3단계: 로고 전용 필터 (요구사항 1번)
# ─────────────────────────────────────────────────────────────────
def apply_logo_filters(items: list) -> list:
    """
    로고 타입 항목에만 적용:
    - 공공기관 키워드 → 허용 (하드코딩 + DB official_institutions)
    - 제안사 로고(위반/주의) → 판정 유지
    메시지 규칙 (요구사항 8번):
    - 허용: "발주기관/공공기관 로고로 확인되어 허용 처리"
    - 위반: "레퍼런스 로고 재비교 일치 – 위반 확정" (변경 없음)
    """
    from services.claude_judge import _is_logo_type, _PUBLIC_ORG_KEYWORDS
    from core.config import load_dict as _ld

    # DB official_institutions 로드 (1회)
    _db_official: list = []
    try:
        _db_official = _ld().get("allowed_terms", {}).get("official_institutions", [])
    except Exception:
        pass

    # 전체 공공기관 키워드 = 하드코딩 + extra + DB
    _all_pub_kw = list(_PUBLIC_ORG_KEYWORDS) + list(_PUBLIC_ORG_KW_EXTRA)
    _all_pub_kw += [str(x).strip() for x in _db_official if x]

    out = []
    for it in items:
        dtype   = it.get("type", "기타")
        content = (it.get("content") or "").strip()

        # 로고 타입 아니면 그대로 통과
        if not (_is_logo_type(dtype) or _is_logo_type(content)):
            out.append(it)
            continue

        # 이미 허용으로 처리된 경우 reason 메시지 정비 후 통과
        if it.get("judgment") == "허용":
            it = dict(it)
            # 잘못된 메시지("일반 명사" 등)가 들어 있으면 교체 (요구사항 8번)
            reason = it.get("reason", "")
            if "일반 명사" in reason or "기관명의 일부" in reason:
                it["reason"] = "레퍼런스 로고 재비교 불일치로 허용 처리"
            out.append(it)
            continue

        # 공공기관 키워드 검사
        reason_ctx = (it.get("reason") or "") + " " + content
        matched_kw = None
        for kw in _all_pub_kw:
            if kw and kw.lower() in reason_ctx.lower():
                matched_kw = kw
                break

        if matched_kw:
            it = dict(it)
            it["judgment"]       = "허용"
            it["reason"]         = f"발주기관/공공기관 로고로 확인되어 허용 처리 ({matched_kw})"
            it["recommendation"] = ""
        # 제안사 로고(위반/주의)는 판정 그대로 유지

        out.append(it)
    return out


# ─────────────────────────────────────────────────────────────────
# 4단계: 얼굴/인물사진 오탐 필터 (요구사항 4번)
# ─────────────────────────────────────────────────────────────────
def apply_face_filters(items: list) -> list:
    """
    인물사진/person_candidate 타입 항목에만 적용.

    ★ 핵심 원칙 — person_candidate ≠ 실제 사람 사진 확정
    ──────────────────────────────────────────────────────
    Claude Vision의 person_candidate 반환은 "후보 탐지"일 뿐이며,
    실제 사람 사진 확정은 반드시 MediaPipe/이미지 후처리(_post_process_faces) 결과에
    의해서만 판정된다.

    절대 금지:
      - person_candidate 타입만 보고 위반/사람사진 페이지로 확정
      - reason 문자열("실제 사람 사진", "카메라로 촬영" 등)만 보고 위반 확정
      - _REAL_PHOTO_KW_SP 키워드 탐지 → 즉시 위반 확정

    ★ 처리 흐름:
      0. _post_process_faces(_face_reverified=True) 재검증 완료 항목
         → MediaPipe 판정 결과를 그대로 신뢰, 중복 처리 금지
      1. 이미 허용 → 통과
      2. 그래픽/아이콘 키워드 존재 → 즉시 허용
         (content+dtype 기준, reason은 오허용 방지를 위해 제외)
      3. _face_reverified 없는 나머지 항목 → 원래 판정 유지
         (문자열로 위반 확정 금지; 입력 judgment 그대로 보존)

    ★ 변수 의미 분리:
      has_person_candidate — Claude가 사람 관련 요소를 후보로 탐지했는가
      page_has_real_face   — MediaPipe/후처리로 실제 얼굴이 확정됐는가
      → 후속 정책은 반드시 page_has_real_face(_face_reverified+real_photo) 기준으로만.
    """
    import logging as _lg
    _face_log = _lg.getLogger(__name__)

    out = []
    for it in items:
        dtype    = it.get("type", "")
        content  = (it.get("content") or "")
        judgment = it.get("judgment", "주의")

        # 얼굴/인물 타입 아니면 통과 (person_candidate 포함)
        is_face_type = any(kw in dtype for kw in _FACE_DTYPES_KW)
        if not is_face_type:
            out.append(it)
            continue

        # ★ 0. _post_process_faces 재검증 완료 항목 → 판정 그대로 신뢰
        #    real_photo → 위반, icon_or_silhouette/unknown → 허용 이미 처리됨
        if it.get("_face_reverified"):
            out.append(it)
            continue

        # 이미 허용이면 통과
        if judgment == "허용":
            out.append(it)
            continue

        # ① 그래픽/아이콘 키워드 (content+dtype 기준만 — reason 제외)
        #    reason에는 범용 표현이 섞여 오허용 발생 가능
        content_only = (dtype + " " + content).lower()
        if any(kw in content_only for kw in _ICON_ALLOW_KW):
            it = dict(it)
            it["judgment"]       = "허용"
            it["reason"]         = "그래픽/아이콘/일러스트 키워드 확인 → 즉시 허용 (후보 탐지였으나 그래픽 확정)"
            it["recommendation"] = ""
            it["_fp_filtered"]   = "icon_silhouette"
            _face_log.debug(
                f"[face_filters] 아이콘 키워드 → 허용: "
                f"dtype={dtype!r} content={content[:40]!r}"
            )
            out.append(it)
            continue

        # ② _face_reverified 없는 나머지 → 원래 판정 유지
        #    ★ 절대금지: 문자열 키워드(_REAL_PHOTO_KW_SP)만 보고 위반 확정하지 않음
        #    page_images 없어 MediaPipe 재검증 불가였던 경우로,
        #    입력 judgment(주의/위반)를 그대로 유지 (안전 방향이나 확정 아님)
        it = dict(it)
        if judgment not in ("위반", "주의"):
            it["judgment"] = "위반"  # 알 수 없는 값은 안전 방향으로 위반
        it["reason"] = (
            (it.get("reason") or "")
            + " [person_candidate 후보 — MediaPipe 재검증 미수행, 판정 유보]"
        )
        _face_log.debug(
            f"[face_filters] 재검증 미수행 → 판정 유보({it['judgment']}): "
            f"dtype={dtype!r} content={content[:40]!r}"
        )
        out.append(it)

    alog.log("server_pipeline", "face_filter_done", {
        "input_count":  len(items),
        "output_count": len(out),
        "results": [
            {
                "page":       it.get("page"),
                "type":       it.get("type"),
                "judgment":   it.get("judgment"),
                "reverified": it.get("_face_reverified", False),
                "content":    str(it.get("content", ""))[:60],
            }
            for it in out
            if any(kw in str(it.get("type", "")).lower()
                   for kw in ("인물", "사진", "얼굴", "face", "photo", "person", "candidate"))
        ],
    })
    return out


# ─────────────────────────────────────────────────────────────────
# 5단계: Rule + Vision 병합 (요구사항 6번 우선순위)
# ─────────────────────────────────────────────────────────────────
def merge_rule_and_vision(
    vision_items: list,
    rule_hits_by_page: dict,
) -> dict:
    """
    Vision 항목 → page_map 구성 후 rule_hits 병합.

    우선순위:
    1순위: 사전 등록 실명 rule (source='rule', type='참여인력명'/'대표자명')
    2순위: 이메일/URL rule
    3순위: 로고 후처리 결과 (rule verdict 변경 금지)
    4순위: Claude Vision 판정
    5순위: OCR rule (source='ocr')
    """
    from services.claude_judge import _is_logo_type

    _WEIGHT = {"위반": 2, "주의": 1, "허용": 0}

    # 우선순위 가중치 — rule 소스별
    def _rule_priority(h: dict) -> int:
        src  = h.get("source", "rule")
        typ  = h.get("type", "")
        jdg  = h.get("judgment", "주의")
        # 사전 등록 실명 rule → 최고 우선순위
        if src in ("rule",) and typ in ("참여인력명", "대표자명", "업체명"):
            return 10
        # 이메일/URL
        if src == "rule" and any(k in typ for k in ("이메일", "URL", "도메인")):
            return 9
        # OCR 기반 rule
        if src.startswith("ocr"):
            return 5
        return 7   # 일반 rule

    page_map: dict[int, list] = {}

    # Vision → page_map
    for it in vision_items:
        p = it.get("_page_int", 1)
        page_map.setdefault(p, []).append({
            "detection_type":  it.get("type",           "기타"),
            "detected_text":   it.get("content",        ""),
            "verdict":         it.get("judgment",        "주의"),
            "reason":          it.get("reason",          ""),
            "recommendation":  it.get("recommendation",  ""),
            "confidence":      it.get("confidence",      0.9),
            "source":          "vision",
            "_fp_filtered":    it.get("_fp_filtered",    ""),
            "_is_logo":        (
                _is_logo_type(it.get("type", ""))
                or _is_logo_type(it.get("content", ""))
            ),
        })

    # rule_hits 병합
    for page_str, hits in rule_hits_by_page.items():
        try:
            p = int(page_str)
        except ValueError:
            continue

        for h in hits:
            h_content  = (h.get("content") or "").lower()
            h_judgment = h.get("judgment", "주의")
            rule_src   = h.get("source", "rule")

            # 기존 vision 항목과 텍스트 매칭
            matched_idx = None
            for idx, d in enumerate(page_map.get(p, [])):
                vc = d["detected_text"].lower()
                if h_content and vc and (
                    h_content == vc
                    or (h_content in vc and len(h_content) >= 4
                        and len(h_content) / len(vc) > 0.5)
                    or (vc in h_content and len(vc) >= 4
                        and len(vc) / len(h_content) > 0.5)
                ):
                    matched_idx = idx
                    break

            if matched_idx is not None:
                existing = page_map[p][matched_idx]
                # source 통합
                if existing.get("source") == "vision":
                    existing["source"] = f"{rule_src}+vision"

                # ★ 로고 타입 → rule verdict 변경 절대 금지 (요구사항 6번)
                if existing.get("_is_logo"):
                    continue

                # rule 우선순위 vs 기존 verdict
                r_prio = _rule_priority(h)
                if r_prio >= 9:
                    # 최고 우선순위 rule(사전 실명/이메일/URL) → 무조건 강한 쪽
                    if _WEIGHT.get(h_judgment, 0) > _WEIGHT.get(existing["verdict"], 0):
                        existing["verdict"] = h_judgment
                else:
                    # 일반 rule → 더 강한 쪽
                    if _WEIGHT.get(h_judgment, 0) > _WEIGHT.get(existing["verdict"], 0):
                        existing["verdict"] = h_judgment
            else:
                # 신규 항목
                page_map.setdefault(p, []).append({
                    "detection_type":  h.get("type",           "기타"),
                    "detected_text":   h.get("content",        ""),
                    "verdict":         h_judgment,
                    "reason":          h.get("reason",          ""),
                    "recommendation":  h.get("recommendation",  ""),
                    "confidence":      h.get("confidence",      0.95),
                    "source":          rule_src,
                    "_fp_filtered":    "",
                    "_is_logo":        False,
                })

    # 분석 로그: merge 결과
    total_items = sum(len(v) for v in page_map.values())
    verdict_counts: dict = {}
    for items in page_map.values():
        for it in items:
            j = it.get("verdict") or it.get("judgment") or "?"
            verdict_counts[j] = verdict_counts.get(j, 0) + 1
    alog.log("server_pipeline", "merge_done", {
        "pages":          list(page_map.keys()),
        "total_items":    total_items,
        "verdict_counts": verdict_counts,
    })

    return page_map


# ─────────────────────────────────────────────────────────────────
# 보조: 후보/확정 분리 집계 (has_person_candidate / page_has_real_face)
# ─────────────────────────────────────────────────────────────────
def compute_face_page_flags(vision_items: list) -> tuple[dict, dict]:
    """
    Vision 항목 리스트에서 페이지별 person_candidate 여부와 실제 얼굴 확정 여부를 집계.

    반환:
      has_person_candidate : {page_int: True}  — Claude가 후보를 탐지한 페이지
      page_has_real_face   : {page_int: True}  — MediaPipe 재검증으로 실제 얼굴이 확정된 페이지

    ★ 중요:
      - has_person_candidate 만으로 페이지 스킵/로고 면제 등 판단 금지
      - page_has_real_face 기준으로만 후속 정책 결정
    """
    has_person_candidate: dict[int, bool] = {}
    page_has_real_face:   dict[int, bool] = {}

    for it in vision_items:
        try:
            pg = int(it.get("page", 0))
        except (ValueError, TypeError):
            pg = 0
        if pg < 1:
            continue

        dtype = str(it.get("type") or "")
        # 후보 탐지 여부
        if "person_candidate" in dtype or any(
            kw in dtype for kw in ("인물", "사진", "얼굴", "face", "photo", "person")
        ):
            has_person_candidate[pg] = True

        # 실제 얼굴 확정: MediaPipe 재검증 완료 + 위반 판정
        if it.get("_face_reverified") and it.get("judgment") == "위반":
            page_has_real_face[pg] = True

    return has_person_candidate, page_has_real_face


# ─────────────────────────────────────────────────────────────────
# 6단계: 내부 키 정리 후 최종 page_map 반환
# ─────────────────────────────────────────────────────────────────
def finalize_page_map(page_map: dict) -> dict:
    """_page_int, _fp_filtered, _is_logo 등 내부 키 제거"""
    _internal = ("_page_int", "_fp_filtered", "_is_logo")
    final: dict[int, list] = {}
    for p, dets in page_map.items():
        cleaned = []
        for d in dets:
            d2 = {k: v for k, v in d.items() if k not in _internal}
            cleaned.append(d2)
        final[p] = cleaned
    return final


# ─────────────────────────────────────────────────────────────────
# 최종 진입점: _merge_results (6단계 파이프라인 호출)
# ─────────────────────────────────────────────────────────────────
def _merge_results(rule_hits_by_page: dict, vision_items: list, total_pages: int) -> dict:
    """
    규칙 탐지(OCR 포함) + Vision 결과 페이지별 합산.
    내부적으로 6단계 함수로 분리 실행.
    """
    import logging as _lg
    _mrlog = _lg.getLogger(__name__)

    # 1. 정규화
    items = normalize_vision_items(vision_items)
    # 2. 텍스트 오탐 필터 (비로고 전용)
    items = apply_text_fp_filters(items)
    # 3. 로고 필터 (로고 전용)
    items = apply_logo_filters(items)
    # 4. 얼굴 필터
    items = apply_face_filters(items)

    # ★ 후보/확정 분리 집계 (has_person_candidate vs page_has_real_face)
    has_pc, has_rf = compute_face_page_flags(items)
    for pg in sorted(set(has_pc) | set(has_rf)):
        cand = has_pc.get(pg, False)
        real = has_rf.get(pg, False)
        if cand and not real:
            _mrlog.info(
                f"[merge_results] p{pg}: person_candidate 후보만 있음 "
                f"(page_has_real_face=False) → 스킵/로고면제 절대 금지"
            )
        elif real:
            _mrlog.info(
                f"[merge_results] p{pg}: 실제 얼굴 확정 (page_has_real_face=True)"
            )

    # 5. rule 병합
    page_map = merge_rule_and_vision(items, rule_hits_by_page)
    # 6. 내부 키 정리
    return finalize_page_map(page_map)


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
        "created_at":              now_kst_iso(),
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


def _load_logo_symbol_b64() -> Optional[str]:
    """저장된 로고 심볼 레퍼런스 이미지 로드 (logo_symbol_reference.png)"""
    sym_path = DATA_DIR / "logo_symbol_reference.png"
    if sym_path.exists():
        try:
            return base64.b64encode(sym_path.read_bytes()).decode("utf-8")
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
