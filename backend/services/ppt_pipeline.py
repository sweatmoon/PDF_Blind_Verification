"""
PPT(X) 서버사이드 검증 파이프라인
server_pipeline.py 와 동일한 report dict 구조 반환
1. python-pptx 텍스트 직접 추출 (텍스트박스/표/노트/하이퍼링크)
2. 슬라이드 내 이미지 Google Vision OCR
3. Claude Vision AI (슬라이드 이미지 배치)
"""
from __future__ import annotations
import asyncio, base64, io, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from core.config import now_kst_iso
from pathlib import Path
from typing import List, Optional

from services.rule_detector import get_rule_detector, _is_org_context
from services.claude_judge  import get_claude_judge, ClaudeVisionJudge, scan_slide_for_faces, verify_pil_against_logo
from services.ocr_service   import get_ocr
from services.ppt_service   import PPTService
from services.file_manager  import _wipe_file
from core.config import get_logger, update_job, load_dict, DATA_DIR

logger = get_logger("ppt_pipeline")

PAGES_PER_BATCH = 1    # Claude Vision 배치당 슬라이드 수 (1=오탐 방지)
GV_BATCH_SIZE   = 16   # Google Vision 배치당 최대 이미지 수
MAX_IMG_OCR     = 10   # 슬라이드당 이미지 OCR 최대 개수
_executor = ThreadPoolExecutor(max_workers=8)


def _pil_to_b64_jpeg(pil_img, quality: int = 80) -> str:
    """PIL Image → JPEG base64 문자열"""
    buf = io.BytesIO()
    if pil_img.mode not in ("RGB", "L"):
        pil_img = pil_img.convert("RGB")
    pil_img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _load_logo_b64() -> Optional[str]:
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


class PPTServerPipeline:
    """PPT(X) 서버사이드 완전 검증 파이프라인"""

    def __init__(self):
        self.rules = get_rule_detector()
        self.judge: ClaudeVisionJudge = get_claude_judge()
        self.ocr   = get_ocr()

    async def run(self, job_id: str, pptx_path: Path, filename: str) -> dict:
        t0 = time.time()
        logger.info(f"[{job_id}] PPT 파이프라인 시작: {filename}")

        def prog(pct: int, msg: str):
            update_job(job_id, progress=pct, message=msg)

        loop = asyncio.get_event_loop()

        # ── 1. PPTX 열기 ──────────────────────────────────────
        prog(5, "PPTX 파싱 중…")
        svc = PPTService(pptx_path)
        if not svc.open():
            raise RuntimeError("PPTX 파일을 열 수 없습니다.")

        total     = svc.total_slides
        claude_on = self.judge.enabled and getattr(self.judge, '_client', None) is not None
        ocr_on    = self.ocr.enabled
        gv_on     = self.ocr.use_google_vision

        mode_desc = []
        if claude_on: mode_desc.append("Claude Vision")
        if ocr_on:    mode_desc.append("Google Vision OCR" if gv_on else "Tesseract OCR")
        mode_desc.append("규칙 탐지")

        logger.info(f"[{job_id}] {total}슬라이드 | 모드: {' + '.join(mode_desc)}")
        prog(8, f"총 {total}슬라이드 · 텍스트 직접 추출 시작…")

        # ── 2. 전체 슬라이드 텍스트 직접 추출 ─────────────────
        prog(10, "텍스트 추출 중… (python-pptx)")
        slide_texts:  dict[int, str]  = {}   # idx → 텍스트
        slide_images: dict[int, list] = {}   # idx → [image_dict, ...]
        slide_hidden: set[int]        = set()

        for i in range(total):
            sd = svc.extract_slide(i)
            slide_texts[i]  = sd.text
            slide_images[i] = sd.images[:MAX_IMG_OCR]
            if sd.is_hidden:
                slide_hidden.add(i)
            # 하이퍼링크도 텍스트에 포함
            if sd.hyperlinks:
                slide_texts[i] += "\n" + "\n".join(sd.hyperlinks)
            if i % 10 == 0:
                await asyncio.sleep(0)

        text_count = sum(1 for t in slide_texts.values() if t.strip())
        img_count  = sum(len(v) for v in slide_images.values())
        logger.info(f"[{job_id}] 텍스트 있는 슬라이드: {text_count}/{total}, 이미지 수: {img_count}")
        prog(20, f"텍스트 추출 완료 ({text_count}/{total}슬라이드) · OCR 시작…")

        # ── 3. 슬라이드 이미지 OCR (Google Vision 배치) ────────
        ocr_results: dict[tuple, str]      = {}  # (slide_idx, img_idx) → text
        ocr_cropped_flags: dict[tuple, bool] = {}  # (slide_idx, img_idx) → 크롭 여부

        # OCR 대상 이미지 수집 (60px 이상)
        # (i, j, pil, is_cropped) 형태 — 크롭 여부 함께 전달
        ocr_targets: List[tuple] = []
        for i in range(total):
            for j, img_d in enumerate(slide_images.get(i, [])):
                w, h       = img_d.get("w", 0), img_d.get("h", 0)
                pil        = img_d.get("pil")
                is_cropped = img_d.get("is_cropped", False)
                if pil and w >= 60 and h >= 60:
                    ocr_targets.append((i, j, pil, is_cropped))

        if ocr_targets and ocr_on:
            engine = "Google Vision" if gv_on else "Tesseract"
            prog(22, f"이미지 OCR 시작… ({len(ocr_targets)}개 · {engine})")

            if gv_on:
                # Google Vision 배치 처리
                batches = [
                    ocr_targets[s:s + GV_BATCH_SIZE]
                    for s in range(0, len(ocr_targets), GV_BATCH_SIZE)
                ]

                def _gv_batch(items):
                    # gv_ocr_batch는 [(key, pil), ...] 형식
                    gv_input = [(k, pil) for k, (_, _, pil, _c) in enumerate(items)]
                    return self.ocr.gv_ocr_batch(gv_input)

                batch_tasks = [
                    loop.run_in_executor(_executor, _gv_batch, batch)
                    for batch in batches
                ]
                batch_results = await asyncio.gather(*batch_tasks)

                for batch, result in zip(batches, batch_results):
                    for k, text in result.items():
                        si, ii, _pil, is_cropped = batch[k]
                        if text.strip():
                            ocr_results[(si, ii)] = text
                            # 크롭된 이미지에서 검출된 텍스트임을 표시
                            if is_cropped:
                                ocr_cropped_flags[(si, ii)] = True

                prog(40, f"Google Vision OCR 완료 ({len(ocr_targets)}개 이미지)")

            else:
                # Tesseract 폴백
                for k, (si, ii, pil, is_cropped) in enumerate(ocr_targets):
                    text = await loop.run_in_executor(
                        _executor, self.ocr.from_image, pil)
                    if text and text.strip():
                        ocr_results[(si, ii)] = text
                        if is_cropped:
                            ocr_cropped_flags[(si, ii)] = True
                    pct = 22 + int(k / len(ocr_targets) * 18)
                    prog(min(pct, 40), f"Tesseract OCR… {k+1}/{len(ocr_targets)}")

            ocr_hit = len(ocr_results)
            logger.info(f"[{job_id}] OCR 완료: {len(ocr_targets)}개 이미지 → {ocr_hit}개 텍스트 추출")

        # ── 4. 규칙 탐지 (텍스트 + OCR 결과) ──────────────────
        prog(42, "규칙 탐지 중…")
        rule_hits_by_page: dict[str, list] = {}

        for i in range(total):
            slide_num = i + 1
            combined_text = slide_texts.get(i, "")

            # OCR 텍스트 병합 (크롭 여부도 추적)
            ocr_texts_for_slide  = []   # (text, is_cropped) 튜플 리스트
            for j in range(len(slide_images.get(i, []))):
                ot         = ocr_results.get((i, j), "")
                is_cropped = ocr_cropped_flags.get((i, j), False)
                if ot.strip():
                    ocr_texts_for_slide.append((ot, is_cropped))

            # 텍스트 + OCR 합산 (크롭 여부 무관하게 모두 탐지)
            full_text = combined_text
            if ocr_texts_for_slide:
                full_text += "\n" + "\n".join(t for t, _ in ocr_texts_for_slide)

            # 크롭된 OCR 텍스트 집합 (판정 강등용)
            cropped_ocr_texts = {t for t, c in ocr_texts_for_slide if c}

            # 숨겨진 슬라이드 경고
            hidden_hits = []
            if i in slide_hidden and combined_text.strip():
                hidden_hits.append({
                    "type":           "간접식별",
                    "content":        f"[숨겨진 슬라이드] {combined_text[:80]}",
                    "judgment":       "주의",
                    "reason":         "숨겨진 슬라이드에 텍스트 존재 – 제출 시 노출 위험",
                    "recommendation": "숨겨진 슬라이드 삭제 또는 내용 확인",
                    "confidence":     0.85,
                    "source":         "rule",
                })

            hits = self.rules.detect(full_text, slide_num)
            all_hits = list(hidden_hits)
            for h in hits:
                det_text = h.detected_text or ""
                verdict  = h.verdict.value

                # OCR에서 검출됐는지 판별
                from_ocr = any(
                    det_text.strip() in ot
                    for ot, _ in ocr_texts_for_slide
                ) if det_text else False

                # 크롭된 이미지 OCR에서만 검출 + 텍스트 레이어에 없음 → '주의'로 강등
                from_cropped_only = (
                    from_ocr and
                    bool(det_text) and
                    any(det_text.strip() in ct for ct in cropped_ocr_texts) and
                    det_text.strip() not in combined_text
                )
                if from_cropped_only and verdict == "위반":
                    verdict  = "주의"
                    h_reason = (h.reason or "") + " [크롭 영역 밖 – 화면에 보이지 않음]"
                    h_rec    = "크롭된 이미지의 숨겨진 영역에서 검출 – 직접 노출 아님, 이미지 재편집 권장"
                else:
                    h_reason = h.reason
                    h_rec    = h.recommendation

                all_hits.append({
                    "type":           h.detection_type.value,
                    "content":        det_text,
                    "judgment":       verdict,
                    "reason":         h_reason,
                    "recommendation": h_rec,
                    "confidence":     h.confidence,
                    "source":         "ocr" if from_ocr else "rule",
                    "cropped":        from_cropped_only,
                })

            if all_hits:
                rule_hits_by_page[str(slide_num)] = all_hits

            if i % 20 == 0:
                await asyncio.sleep(0)

        rule_total = sum(len(v) for v in rule_hits_by_page.values())
        logger.info(f"[{job_id}] 규칙 탐지 완료: {rule_total}건")
        # 규칙 탐지 상세 로그
        for _pg, _hits in rule_hits_by_page.items():
            for _h in _hits:
                logger.info(
                    f"[{job_id}] rule_hit | page={_pg} "
                    f"type={_h.get('type','?')} "
                    f"judgment={_h.get('judgment','?')} "
                    f"content={str(_h.get('content',''))[:40]!r}"
                )

        # ── 4-B. 슬라이드 마스터 / 레이아웃 텍스트 규칙 탐지 ────────
        # 슬라이드 본문에 이미 나온 텍스트는 제외 (중복 탐지 방지)
        master_text_hits: list[dict] = []   # source=master 탐지 결과
        layout_text_hits: list[dict] = []   # source=layout 탐지 결과
        try:
            lm_texts = svc.extract_layout_master_texts()
            logger.info(f"[{job_id}] 마스터/레이아웃 텍스트 {len(lm_texts)}개 규칙 탐지 시작")

            # 슬라이드 본문에 이미 등장한 텍스트 집합 (중복 방지용)
            all_slide_texts: set[str] = set()
            for i in range(total):
                for line in (slide_texts.get(i, "")).splitlines():
                    s = line.strip()
                    if s:
                        all_slide_texts.add(s)

            for lm in lm_texts:
                t        = lm["text"]
                source   = lm["source"]           # "master" | "layout"
                affected = lm["affected_slides"]   # [slide_num, ...]

                # 슬라이드 본문에 이미 있는 텍스트 스킵
                if t in all_slide_texts:
                    continue

                # 대표 슬라이드 번호 (없으면 0 → 가상 마스터 페이지 -1용)
                repr_slide = affected[0] if affected else 0

                hits = self.rules.detect(t, repr_slide)
                for h in hits:
                    det_text = h.detected_text or ""
                    verdict  = h.verdict.value
                    entry = {
                        "type":           h.detection_type.value,
                        "content":        det_text,
                        "judgment":       verdict,
                        "reason":         (h.reason or "") + f" [슬라이드 {source}에서 탐지]",
                        "recommendation": h.recommendation or "슬라이드 마스터/레이아웃에서 해당 텍스트를 제거하세요.",
                        "confidence":     h.confidence,
                        "source":         f"rule/{source}",
                        "struct_source":  source,
                        "affected_pages": affected,
                        "_struct_logo":   False,
                        "_shape_name":    "",
                        "_logo_case":     "",
                    }
                    if source == "master":
                        master_text_hits.append(entry)
                    else:
                        layout_text_hits.append(entry)
                    logger.info(
                        f"[{job_id}] master_rule_hit | source={source} "
                        f"type={h.detection_type.value} "
                        f"judgment={verdict} "
                        f"content={det_text[:40]!r} "
                        f"affected={len(affected)}슬라이드"
                    )

            m_total = len(master_text_hits) + len(layout_text_hits)
            logger.info(f"[{job_id}] 마스터/레이아웃 규칙 탐지 완료: {m_total}건 "
                        f"(master={len(master_text_hits)}, layout={len(layout_text_hits)})")
        except Exception as e:
            logger.warning(f"[{job_id}] 마스터/레이아웃 텍스트 규칙 탐지 실패: {e}")

        prog(50, f"규칙 탐지 {rule_total}건 · Vision AI 분석 시작…")

        # ── 5. 슬라이드 이미지 렌더링 (Claude Vision용) ─────────
        page_images = []   # [{"page": int, "b64": str, "media_type": str}]

        if claude_on:
            prog(50, f"슬라이드 이미지 렌더링 중 (0/{total})…")

            def _render_slide(idx: int) -> Optional[str]:
                try:
                    pil = svc.render_for_vision(idx)
                    if pil is None:
                        return None
                    return _pil_to_b64_jpeg(pil, quality=80)
                except Exception as e:
                    logger.warning(f"슬라이드 {idx+1} 렌더링 실패: {e}")
                    return None

            RENDER_BATCH = 4
            for batch_start in range(0, total, RENDER_BATCH):
                batch_end = min(batch_start + RENDER_BATCH, total)
                tasks = [
                    loop.run_in_executor(_executor, _render_slide, i)
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
                pct = 50 + int((batch_end / total) * 15)
                prog(pct, f"슬라이드 렌더링 중 ({batch_end}/{total})…")
                await asyncio.sleep(0)

            prog(65, f"{len(page_images)}/{total}슬라이드 변환 완료 · Claude Vision 분석 시작…")
        else:
            prog(65, "Claude Vision 비활성 — 규칙+OCR 결과로 리포트 생성…")

        # ── 6. Claude Vision 배치 분석 ─────────────────────────
        all_vision_items: list[dict] = []

        if claude_on and page_images:
            company_dict    = load_dict()
            logo_b64        = _load_logo_b64()
            logo_symbol_b64 = _load_logo_symbol_b64()
            batches      = [page_images[i:i+PAGES_PER_BATCH]
                            for i in range(0, len(page_images), PAGES_PER_BATCH)]
            batch_count  = len(batches)

            BATCH_CONCURRENCY = 3   # 5→3: 429 rate limit 방지
            sem       = asyncio.Semaphore(BATCH_CONCURRENCY)
            completed = [0]

            async def run_batch(bi: int, batch: list):
                async with sem:
                    batch_rule_hits = {
                        str(pg["page"]): rule_hits_by_page[str(pg["page"])]
                        for pg in batch
                        if str(pg["page"]) in rule_hits_by_page
                    }
                    # 429 rate limit 자동 재시도 (최대 2회, 지수 백오프)
                    result = []
                    for _attempt in range(3):
                        try:
                            import functools as _ft
                            _fn = _ft.partial(
                                self.judge.judge_image_batch,
                                batch, logo_b64, company_dict,
                                batch_rule_hits or None,
                                logo_symbol_b64,
                            )
                            result = await loop.run_in_executor(_executor, _fn)
                            logger.info(f"[{job_id}] Vision 배치 {bi+1}/{batch_count}: {len(result)}건")
                            break
                        except Exception as e:
                            err_str = str(e)
                            if "429" in err_str and _attempt < 2:
                                wait_sec = 10 * (2 ** _attempt)  # 10s, 20s
                                logger.warning(
                                    f"[{job_id}] Vision 배치 {bi+1} 429 rate limit "
                                    f"→ {wait_sec}s 후 재시도 ({_attempt+1}/2)"
                                )
                                await asyncio.sleep(wait_sec)
                                continue
                            logger.error(f"[{job_id}] Vision 배치 {bi+1} 오류: {e}")
                            result = []
                            break
                    completed[0] += 1
                    pct = 65 + int((completed[0] / batch_count) * 25)
                    prog(pct, f"Claude Vision 분석 중 ({completed[0]}/{batch_count} 배치)…")
                    return result

            vision_tasks   = [run_batch(bi, b) for bi, b in enumerate(batches)]
            vision_results = await asyncio.gather(*vision_tasks)
            for items in vision_results:
                all_vision_items.extend(items)

            logger.info(f"[{job_id}] Claude Vision 완료: {len(all_vision_items)}건")

        # ── 6-B. 독립 Face Scan 패스 ───────────────────────────────────
        # Claude가 person_candidate를 빠뜨린 경우를 보완:
        # 슬라이드 전체 이미지에서 MediaPipe + Haar로 직접 얼굴을 탐지.
        # 이미 Claude가 잡은 bbox와 IoU > 0.3 겹치는 경우는 자동 중복 제거.
        if page_images:
            # 기존 person_candidate bbox 수집 (중복 방지용)
            existing_pc_bboxes: dict[int, list] = {}
            for vi in all_vision_items:
                if vi.get("type") == "person_candidate":
                    pg = int(vi.get("page", 0))
                    bb = vi.get("bbox")
                    if bb:
                        existing_pc_bboxes.setdefault(pg, []).append(bb)

            direct_scan_items: list[dict] = []
            _judge_client = getattr(self.judge, "client", None)
            _judge_model  = getattr(self.judge, "model", "")

            for pg_info in page_images:
                pg_no  = pg_info["page"]
                pg_b64 = pg_info.get("b64", "")
                if not pg_b64:
                    continue

                existing_bbs = existing_pc_bboxes.get(pg_no, [])
                new_items = scan_slide_for_faces(
                    pg_b64,
                    pg_no,
                    existing_bboxes=existing_bbs,
                    client=_judge_client,
                    model=_judge_model,
                )
                if new_items:
                    logger.info(
                        f"[{job_id}] 직접 face scan p{pg_no}: "
                        f"{len(new_items)}건 추가"
                    )
                    direct_scan_items.extend(new_items)

            if direct_scan_items:
                logger.info(
                    f"[{job_id}] 직접 face scan 합계: {len(direct_scan_items)}건 추가"
                )
                all_vision_items.extend(direct_scan_items)
            else:
                logger.debug(f"[{job_id}] 직접 face scan: 추가 탐지 없음")

        # ── 6-C. Layout / Master 구조적 로고 탐지 ──────────────────────
        # 렌더링된 슬라이드 이미지가 아닌 PPT 내부 이미지 객체를 직접 추출해 비교.
        # 슬라이드 본문(source=slide), 레이아웃(source=layout), 마스터(source=master)
        # 각각에서 이미지를 추출하고 레퍼런스 로고와 pHash/SSIM/ORB/red_mask 비교.
        prog(90, "Layout/Master 구조적 로고 탐지 중…")
        struct_logo_items: list[dict] = []

        logo_b64        = _load_logo_b64()
        logo_symbol_b64 = _load_logo_symbol_b64()

        # 6-C 실행 상태 항상 로그 (디버그용)
        slide_img_count = sum(
            1 for imgs in slide_images.values()
            for img_d in imgs if img_d.get("pil") is not None
        )
        logger.info(
            f"[{job_id}] 6-C 시작: logo_ref={'있음' if logo_b64 else '없음'} "
            f"symbol_ref={'있음' if logo_symbol_b64 else '없음'} "
            f"slide_img_count={slide_img_count}"
        )

        if logo_b64:
            # ── (a) Slide 본문 이미지 직접 비교 ────────────────────
            slide_checked = 0
            for slide_idx in range(total):
                imgs = slide_images.get(slide_idx, [])
                for img_d in imgs:
                    pil = img_d.get("pil")
                    if pil is None:
                        continue
                    slide_checked += 1
                    match = verify_pil_against_logo(pil, logo_b64, logo_symbol_b64)
                    if match["matched"]:
                        slide_num   = slide_idx + 1
                        is_symbol   = match.get("symbol_matched", False)
                        match_case  = match.get("case", "A")  # "A" | "C"
                        verdict     = match.get("verdict", "위반")  # Case C → "주의"
                        det_type    = "심볼기반 로고" if is_symbol else "로고 (직접)"
                        logger.info(
                            f"[{job_id}] 구조 로고 탐지 slide p{slide_num} "
                            f"case={match_case} method={match['method']} "
                            f"score={match['score']} → {verdict}"
                        )
                        if match_case == "C":
                            reason = (
                                f"[Case C] 슬라이드 본문 이미지: 심볼 유사 (유사도 {match['score']:.2f}) "
                                f"but 전체 로고 불일치 — 수동 확인 필요"
                            )
                            recommendation = "워드마크 포함 여부를 직접 확인하세요. 워드마크 있으면 위반."
                        else:
                            reason = (
                                f"[Case A] 슬라이드 본문 이미지: 레퍼런스 로고 검출 "
                                f"(유사도 {match['score']:.2f})"
                            )
                            recommendation = "해당 이미지를 슬라이드에서 제거하거나 교체하세요."
                        struct_logo_items.append({
                            "page":           slide_num,
                            "type":           det_type,
                            "content":        f"슬라이드 내장 이미지 로고 ({match['method']})",
                            "judgment":       verdict,
                            "reason":         reason,
                            "recommendation": recommendation,
                            "confidence":     min(0.95, 0.75 + match["score"] * 0.2),
                            "source":         "slide",
                            "struct_source":  "slide",
                            "_struct_logo":   True,
                            "_logo_case":     match_case,
                        })

            # ── (b) Layout / Master 이미지 직접 비교 ───────────────
            logger.info(f"[{job_id}] 6-C(a) 슬라이드 직접 비교 완료: {slide_checked}개 이미지 검사")
            try:
                lm_images = svc.extract_layout_master_images()
                logger.info(f"[{job_id}] layout/master 이미지 {len(lm_images)}개 추출")
            except Exception as e:
                logger.warning(f"[{job_id}] layout/master 추출 실패: {e}")
                lm_images = []

            # 슬라이드별로 적용되는 layout 인덱스 수집 (마스터 로고가 어느 슬라이드에 나타나는지)
            layout_to_slides: dict[int, list[int]] = {}  # layout_idx → [slide_num, ...]
            master_to_slides: dict[int, list[int]] = {}  # master_idx → [slide_num, ...]
            try:
                from pptx.enum.shapes import MSO_SHAPE_TYPE as _MSO
                prs = svc._prs
                # 마스터 인덱스 맵: layout → master_idx
                master_list  = list(prs.slide_masters)
                layout_master_map: dict[int, int] = {}  # layout obj id → master_idx
                for mi, master in enumerate(master_list):
                    for layout in master.slide_layouts:
                        layout_master_map[id(layout)] = mi

                for si, slide in enumerate(svc._slides_list):
                    slide_num = si + 1
                    try:
                        lo = slide.slide_layout
                        li = None
                        for mi2, m2 in enumerate(master_list):
                            try:
                                li = list(m2.slide_layouts).index(lo)
                                master_to_slides.setdefault(mi2, []).append(slide_num)
                                layout_to_slides.setdefault(li, []).append(slide_num)
                                break
                            except ValueError:
                                continue
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"[{job_id}] layout/master 슬라이드 매핑 실패: {e}")

            # 이미 슬라이드 직접 탐지에서 잡힌 슬라이드 집합 (마스터 중복 제거용)
            already_detected_slides = {it["page"] for it in struct_logo_items}

            for img_d in lm_images:
                pil    = img_d.get("pil")
                source = img_d.get("source", "layout")  # "layout" | "master"
                s_idx  = img_d.get("source_idx", 0)
                s_name = img_d.get("shape_name", "")
                if pil is None:
                    continue

                match = verify_pil_against_logo(pil, logo_b64, logo_symbol_b64)
                if not match["matched"]:
                    continue

                # 영향받는 슬라이드 번호 목록
                if source == "master":
                    affected = master_to_slides.get(s_idx, list(range(1, total + 1)))
                else:  # layout
                    affected = layout_to_slides.get(s_idx, [])

                if not affected:
                    # 매핑 실패 시 전체 슬라이드에 경고 (1번 페이지로 대표)
                    affected = [1]

                is_symbol  = match.get("symbol_matched", False)
                match_case = match.get("case", "A")    # "A" | "C"
                verdict    = match.get("verdict", "위반")  # Case C → "주의"
                det_type   = "심볼기반 로고" if is_symbol else "로고 (마스터/레이아웃)"
                src_label  = '마스터' if source == 'master' else '레이아웃'

                logger.info(
                    f"[{job_id}] 구조 로고 탐지 {source}[{s_idx}] '{s_name}' "
                    f"case={match_case} method={match['method']} "
                    f"score={match['score']:.3f} → {verdict} "
                    f"({len(affected)}개 슬라이드 영향)"
                )

                if match_case == "C":
                    reason = (
                        f"[Case C] {src_label} 이미지: 심볼 유사 (유사도 {match['score']:.2f}, "
                        f"shape: {s_name or '이름없음'}) but 전체 로고 불일치 — 수동 확인 필요"
                    )
                    recommendation = (
                        f"{src_label} 슬라이드에서 워드마크 포함 여부를 확인하세요. "
                        f"워드마크 있으면 위반. (모든 슬라이드에 공통 적용됨)"
                    )
                else:
                    reason = (
                        f"[Case A] {src_label}에 레퍼런스 로고 이미지 검출 "
                        f"(유사도 {match['score']:.2f}, shape: {s_name or '이름없음'})"
                    )
                    recommendation = (
                        f"{src_label} 슬라이드에서 로고 이미지를 제거하거나 교체하세요. "
                        f"(모든 슬라이드에 공통 적용됨)"
                    )

                # 마스터/레이아웃 로고는 동일 마스터 인덱스+동일 shape 이름 조합만 중복 제거
                # (master[0]과 master[1]에 같은 이름의 shape이 있어도 별도 항목으로 추가)
                repr_page = affected[0] if affected else 1
                dup = any(
                    it.get("struct_source") == source
                    and it.get("_shape_name") == s_name
                    and it.get("_source_idx", -999) == s_idx
                    for it in struct_logo_items
                )
                if not dup:
                    struct_logo_items.append({
                        "page":           repr_page,   # 대표 페이지 1개만
                        "type":           det_type,
                        "content":        f"{source.upper()} 이미지 로고 ({match['method']})",
                        "judgment":       verdict,
                        "reason":         reason,
                        "recommendation": recommendation,
                        "confidence":     min(0.95, 0.80 + match["score"] * 0.15),
                        "source":         source,        # "layout" | "master"
                        "struct_source":  source,
                        "affected_pages": affected,      # 영향받는 슬라이드 전체 목록
                        "_struct_logo":   True,
                        "_logo_case":     match_case,
                        "_shape_name":    s_name,
                        "_source_idx":    s_idx,        # 마스터/레이아웃 인덱스 (중복 체크용)
                    })
        else:
            logger.info(f"[{job_id}] 로고 레퍼런스 없음 → 구조적 로고 탐지 스킵")

        if struct_logo_items:
            # ── 상태 변수 집계 (has_logo_candidate / has_activo_logo / logo_source) ──
            has_logo_candidate = len(struct_logo_items) > 0
            has_activo_logo    = any(
                it.get("judgment") == "위반"
                for it in struct_logo_items
            )
            logo_sources = sorted({it.get("struct_source", "slide") for it in struct_logo_items})

            logger.info(
                f"[{job_id}] 구조적 로고 탐지: {len(struct_logo_items)}건 "
                f"(slide={sum(1 for x in struct_logo_items if x.get('struct_source')=='slide')}, "
                f"layout={sum(1 for x in struct_logo_items if x.get('struct_source')=='layout')}, "
                f"master={sum(1 for x in struct_logo_items if x.get('struct_source')=='master')}) | "
                f"has_logo_candidate={has_logo_candidate} "
                f"has_activo_logo={has_activo_logo} "
                f"logo_source={logo_sources}"
            )
            all_vision_items.extend(struct_logo_items)

        prog(92, "결과 합산 중…")

        # ── 7. 규칙 + Vision 합산 ──────────────────────────────
        # 페이지별 크롭 이미지 여부 맵 생성 (Vision 단독 탐지 결과 강등용)
        page_has_cropped: dict[int, bool] = {}
        for i in range(total):
            for j, img_d in enumerate(slide_images.get(i, [])):
                if img_d.get("is_cropped", False):
                    page_has_cropped[i + 1] = True
                    break

        merged_result = _merge_results(rule_hits_by_page, all_vision_items, total, page_has_cropped)
        merged        = merged_result["page_map"]
        master_items  = merged_result["master_items"]
        layout_items  = merged_result["layout_items"]

        # 4-B에서 수집한 마스터/레이아웃 텍스트 규칙 탐지 결과 병합
        master_items = master_items + master_text_hits
        layout_items = layout_items + layout_text_hits

        # ── 8. 원본 파일 삭제 ──────────────────────────────────
        svc.close()
        try:
            _wipe_file(pptx_path)
        except Exception as e:
            logger.warning(f"[{job_id}] 파일 삭제 실패: {e}")

        # ── 9. 리포트 생성 ─────────────────────────────────────
        prog(95, "리포트 생성 중…")
        elapsed = round(time.time() - t0, 2)
        report  = _build_report(
            job_id, filename, total, merged, elapsed,
            claude_on=claude_on, ocr_on=ocr_on,
            master_items=master_items, layout_items=layout_items)

        prog(100, "검증 완료")
        logger.info(
            f"[{job_id}] 완료 {elapsed}s | "
            f"위반:{report['violation_count']} 주의:{report['caution_count']} | "
            f"모드: {' + '.join(mode_desc)}"
        )
        # 최종 결과 상세 로그
        for _pr in report.get("page_results", []):
            for _det in _pr.get("detections", []):
                if _det.get("verdict") in ("위반", "주의"):
                    logger.info(
                        f"[{job_id}] final_det | page={_pr['page_number']} "
                        f"verdict={_det.get('verdict')} "
                        f"type={_det.get('detection_type','?')} "
                        f"source={_det.get('source','?')} "
                        f"text={str(_det.get('detected_text',''))[:40]!r}"
                    )
        return report


# ── 결과 합산 (server_pipeline 공유 함수 재사용 + PPT 전용 크롭 처리) ──────────
def _merge_results(rule_hits_by_page: dict, vision_items: list, total_slides: int,
                   page_has_cropped: dict = None) -> dict:
    """
    page_has_cropped: {page_number(1-based): True} — 해당 페이지에 크롭 이미지가 있음
    Vision AI가 위반으로 탐지해도 크롭 이미지가 있는 페이지의 로고/업체명은 주의로 강등.

    server_pipeline의 6단계 공유 함수를 재사용하여 동일한 로직 보장:
      1. normalize_vision_items()
      2. apply_text_fp_filters()
      3. apply_logo_filters()
      4. apply_face_filters()
      5+6. PPT 전용 크롭 강등 처리 후 merge_rule_and_vision() + finalize_page_map()
    """
    from services.server_pipeline import (
        normalize_vision_items,
        apply_text_fp_filters,
        apply_logo_filters,
        apply_face_filters,
        finalize_page_map,
    )
    from services.claude_judge import _is_logo_type

    _cropped_pages = page_has_cropped or {}
    _WEIGHT = {"위반": 2, "주의": 1, "허용": 0}

    # ── master / layout 구조 항목 먼저 분리 (page_map에 포함하지 않음) ──
    non_struct_items = [it for it in vision_items if not it.get("_struct_logo")]

    # ── 1~4단계: 공유 필터 파이프라인 ────────────────────────────
    items = normalize_vision_items(non_struct_items)
    items = apply_text_fp_filters(items)
    items = apply_logo_filters(items)
    items = apply_face_filters(items)

    # ── 5단계(PPT 확장): 크롭 이미지 페이지 로고/업체명 위반→주의 강등 ──
    adjusted = []
    for it in items:
        p       = it.get("_page_int", 1)
        dtype   = it.get("type", "기타")
        verdict = it.get("judgment", "주의")
        _cropped = False
        if (_cropped_pages.get(p)
                and verdict == "위반"
                and any(kw in dtype for kw in ("로고", "업체", "회사", "브랜드"))):
            it = dict(it)
            it["judgment"] = "주의"
            it["reason"] = (it.get("reason") or "") + " [크롭 영역 밖 – 화면에 보이지 않음]"
            it["recommendation"] = "크롭된 이미지의 숨겨진 영역에서 검출 – 직접 노출 아님, 이미지 재편집 권장"
            it["_ppt_cropped"] = True
        adjusted.append(it)

    # ── 6단계(PPT 전용): Vision→page_map 구성 + rule_hits 병합 ──
    page_map: dict[int, list] = {}

    for it in adjusted:
        p = it.get("_page_int", 1)
        page_map.setdefault(p, []).append({
            "detection_type":  it.get("type",           "기타"),
            "detected_text":   it.get("content",        ""),
            "verdict":         it.get("judgment",        "주의"),
            "reason":          it.get("reason",          ""),
            "recommendation":  it.get("recommendation",  ""),
            "confidence":      it.get("confidence",      0.9),
            "source":          it.get("source", "vision"),   # layout/master/slide 보존
            "struct_source":   it.get("struct_source",   ""),
            "affected_pages":  it.get("affected_pages",  []),  # 마스터/레이아웃 영향 슬라이드
            "cropped":         it.get("_ppt_cropped",    False),
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

            matched_idx = None
            for idx, d in enumerate(page_map.get(p, [])):
                vc = d["detected_text"].lower()
                if h_content and vc and (
                    h_content == vc
                    or (h_content in vc and len(h_content) >= 4 and len(h_content) / len(vc) > 0.5)
                    or (vc in h_content and len(vc) >= 4 and len(vc) / len(h_content) > 0.5)
                ):
                    matched_idx = idx
                    break

            if matched_idx is not None:
                existing = page_map[p][matched_idx]
                if existing.get("source") == "vision":
                    existing["source"] = f"{rule_src}+vision"

                # ★ 로고 타입 → rule verdict 변경 절대 금지
                if existing.get("_is_logo"):
                    continue

                # cropped=True 인 rule 항목 → 위반이면 주의로 강등
                if h.get("cropped", False):
                    if _WEIGHT.get(existing["verdict"], 0) > 1:
                        existing["verdict"] = "주의"
                        existing["cropped"] = True
                        existing["reason"] = (existing.get("reason") or "") + " [크롭 영역 밖 – 화면에 보이지 않음]"
                        existing["recommendation"] = "크롭된 이미지의 숨겨진 영역에서 검출 – 직접 노출 아님, 이미지 재편집 권장"
                else:
                    if _WEIGHT.get(h_judgment, 0) > _WEIGHT.get(existing["verdict"], 0):
                        existing["verdict"] = h_judgment
            else:
                page_map.setdefault(p, []).append({
                    "detection_type":  h.get("type",           "기타"),
                    "detected_text":   h.get("content",        ""),
                    "verdict":         h_judgment,
                    "reason":          h.get("reason",          ""),
                    "recommendation":  h.get("recommendation",  ""),
                    "confidence":      h.get("confidence",      0.95),
                    "source":          rule_src,
                    "cropped":         h.get("cropped", False),
                    "_fp_filtered":    "",
                    "_is_logo":        False,
                })

    # 내부 키 제거 (+ PPT 전용 _ppt_cropped, _struct_logo, _logo_case, _shape_name)
    # struct_source, affected_pages는 리포트에 포함 (출처/영향 슬라이드 표시용)
    _internal = ("_fp_filtered", "_is_logo", "_ppt_cropped", "_page_int", "_struct_logo", "_logo_case", "_shape_name")
    final: dict[int, list] = {}
    for p, dets in page_map.items():
        final[p] = [{k: v for k, v in d.items() if k not in _internal} for d in dets]

    # ── master / layout 구조 항목 분리 ───────────────────────────────────────
    # page_results 와 별도 섹션으로 반환하기 위해 _final_struct 에 담음.
    # vision_items 원본에서 _struct_logo=True && source in (master, layout) 추출.
    _struct_master: list[dict] = []
    _struct_layout: list[dict] = []
    for it in vision_items:
        if not it.get("_struct_logo"):
            continue
        src = it.get("struct_source") or it.get("source", "")
        entry = {
            "type":           it.get("type", "로고 (마스터/레이아웃)"),
            "content":        it.get("content", ""),
            "judgment":       it.get("judgment", "위반"),
            "reason":         it.get("reason", ""),
            "recommendation": it.get("recommendation", ""),
            "confidence":     it.get("confidence", 0.9),
            "affected_pages": it.get("affected_pages", []),
            "shape_name":     it.get("_shape_name", ""),
            "logo_case":      it.get("_logo_case", "A"),
        }
        if src == "master":
            _struct_master.append(entry)
        elif src == "layout":
            _struct_layout.append(entry)

    return {
        "page_map":      final,
        "master_items":  _struct_master,
        "layout_items":  _struct_layout,
    }


def _build_report(job_id, filename, total_slides, page_map, elapsed,
                  claude_on=False, ocr_on=True,
                  master_items: list = None, layout_items: list = None) -> dict:
    master_items = master_items or []
    layout_items = layout_items or []

    # ── 마스터 / 레이아웃 결과 섹션 구성 ────────────────────────────
    def _struct_section(items: list, source_label: str) -> dict:
        """master_items / layout_items → 리포트 섹션"""
        detections = []
        for it in items:
            detections.append({
                "detection_type":  it.get("type", "로고"),
                "detected_text":   it.get("content", ""),
                "verdict":         it.get("judgment", "위반"),
                "reason":          it.get("reason", ""),
                "recommendation":  it.get("recommendation", ""),
                "confidence":      it.get("confidence", 0.9),
                "affected_pages":  it.get("affected_pages", []),
                "shape_name":      it.get("shape_name", ""),
                "logo_case":       it.get("logo_case", "A"),
                "source":          source_label,
            })
        vc = sum(1 for d in detections if d["verdict"] == "위반")
        cc = sum(1 for d in detections if d["verdict"] == "주의")
        return {
            "source":          source_label,
            "detections":      detections,
            "violation_count": vc,
            "caution_count":   cc,
        }

    master_section = _struct_section(master_items, "master")
    layout_section = _struct_section(layout_items, "layout")

    # ── page_number=-1 가상 페이지: 마스터/레이아웃 탐지 결과 ─────────
    # (page_number=0 은 메타데이터용으로 이미 예약됨)
    # 마스터·레이아웃 detections를 페이지 카드와 동일한 dict 형태로 변환
    def _to_page_det(d: dict) -> dict:
        return {
            "detection_type":  d.get("detection_type", "로고"),
            "detected_text":   d.get("detected_text", ""),
            "verdict":         d.get("verdict", "위반"),
            "reason":          d.get("reason", ""),
            "recommendation":  d.get("recommendation", ""),
            "confidence":      d.get("confidence", 0.9),
            "source":          d.get("source", "master"),
            "struct_source":   d.get("source", "master"),   # "master" | "layout"
            "affected_pages":  d.get("affected_pages", []),
            "cropped":         False,
        }

    struct_dets = (
        [_to_page_det(d) for d in master_section["detections"]]
        + [_to_page_det(d) for d in layout_section["detections"]]
    )

    page_results = []
    # 마스터/레이아웃 카드는 탐지 결과 유무와 관계없이 항상 맨 앞에 삽입
    # (탐지 없으면 초록 "이상없음" 카드, 다른 페이지와 동일한 UX)
    s_vc = sum(1 for d in struct_dets if d["verdict"] == "위반")
    s_cc = sum(1 for d in struct_dets if d["verdict"] == "주의")
    s_ac = sum(1 for d in struct_dets if d["verdict"] == "허용")
    page_results.append({
        "page_number":     -1,         # -1 = 슬라이드 마스터/레이아웃 가상 페이지
        "thumbnail_b64":   None,
        "detections":      struct_dets,
        "violation_count": s_vc,
        "caution_count":   s_cc,
        "allowed_count":   s_ac,
    })

    for p in range(1, total_slides + 1):
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

    # page_number=-1(마스터/레이아웃 가상 페이지) 포함 전체 집계
    # → 이미 page_results 맨 앞에 삽입됐으므로 별도 가산 불필요
    vc_total = sum(p["violation_count"] for p in page_results)
    cc_total = sum(p["caution_count"]   for p in page_results)
    ac_total = sum(p["allowed_count"]   for p in page_results)

    if vc_total >= 5:                    risk = "HIGH"
    elif vc_total >= 1 or cc_total >= 5: risk = "MEDIUM"
    else:                                risk = "LOW"

    all_dets = [d for pr in page_results for d in pr["detections"]]
    # 마스터/레이아웃 detections도 notes 판단에 포함
    all_dets_full = all_dets + master_section["detections"] + layout_section["detections"]

    def has_type(t):
        return any(t in d["detection_type"] and d["verdict"] == "위반" for d in all_dets_full)

    notes = []
    notes.append("업체명 직접 노출 있음"  if has_type("업체") or has_type("회사") else "명확한 업체명 노출 없음")
    notes.append("참여인력 실명 노출 있음" if has_type("인력") or has_type("대표") else "참여인력 실명 없음")
    notes.append("이메일/URL 노출 있음"   if has_type("이메일") or has_type("URL") else "이메일/URL 없음")
    # 마스터에 로고 탐지 시 별도 노트
    if master_section["violation_count"] > 0:
        affected_cnt = sum(len(d.get("affected_pages", [])) for d in master_section["detections"] if d["verdict"] == "위반")
        notes.append(f"슬라이드 마스터에 로고 포함 — 전체 {affected_cnt}개 슬라이드 영향")
    if layout_section["violation_count"] > 0:
        notes.append("슬라이드 레이아웃에 로고 포함")
    if cc_total > 0:
        notes.append(f"간접 식별 가능 표현 {cc_total}건 발견")

    modes = []
    if claude_on: modes.append("Claude Vision AI")
    if ocr_on:    modes.append("OCR")
    modes.append("규칙 탐지")
    analysis_mode = " + ".join(modes)

    flat_items = []
    for pr in page_results:
        for d in pr["detections"]:
            flat_items.append({
                "page":           pr["page_number"],
                "type":           d.get("detection_type", "기타"),
                "content":        d.get("detected_text", ""),
                "judgment":       d.get("verdict", "주의"),
                "reason":         d.get("reason", ""),
                "recommendation": d.get("recommendation", ""),
                "source":         d.get("source", "rule"),
                "struct_source":  d.get("struct_source", ""),   # slide|layout|master
                "affected_pages": d.get("affected_pages", []),  # 마스터/레이아웃 영향 슬라이드
                "cropped":        d.get("cropped", False),
            })

    return {
        "job_id":                  job_id,
        "filename":                filename,
        "total_pages":             total_slides,
        "page_count":              total_slides,
        "processing_time_seconds": elapsed,
        "elapsed_sec":             elapsed,
        "created_at":              now_kst_iso(),
        "risk_level":              risk,
        "violation_count":         vc_total,
        "caution_count":           cc_total,
        "allowed_count":           ac_total,
        "_analysis_mode":          analysis_mode,
        "_file_type":              "pptx",
        "items":                   flat_items,
        "summary_notes":           notes,
        "page_results":            page_results,
        "master_results":          master_section,   # 슬라이드 마스터 검증 결과 (페이지와 별도)
        "layout_results":          layout_section,   # 슬라이드 레이아웃 검증 결과
        "summary": {
            "no_company_name": not (has_type("업체") or has_type("회사")),
            "no_personnel":    not (has_type("인력") or has_type("대표")),
            "no_email_url":    not (has_type("이메일") or has_type("URL")),
            "indirect_count":  cc_total,
            "logo_detected":   any("로고" in d["detection_type"] and d["verdict"] != "허용"
                                   for d in all_dets_full),
            "master_logo_detected": master_section["violation_count"] > 0,
            "metadata_clean":  True,
            "notes":           notes,
        },
    }


# ── 싱글톤 ──────────────────────────────────────────────────────
_inst: PPTServerPipeline | None = None

def get_ppt_pipeline() -> PPTServerPipeline:
    global _inst
    if _inst is None:
        _inst = PPTServerPipeline()
    return _inst
