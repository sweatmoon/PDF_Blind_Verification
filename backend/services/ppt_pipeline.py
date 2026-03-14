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
from pathlib import Path
from typing import List, Optional

from services.rule_detector import get_rule_detector, _is_org_context
from services.claude_judge  import get_claude_judge, ClaudeVisionJudge
from services.ocr_service   import get_ocr
from services.ppt_service   import PPTService
from services.file_manager  import _wipe_file
from core.config import get_logger, update_job, load_dict, DATA_DIR

logger = get_logger("ppt_pipeline")

PAGES_PER_BATCH = 4    # Claude Vision 배치당 슬라이드 수
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
            company_dict = load_dict()
            logo_b64     = _load_logo_b64()
            batches      = [page_images[i:i+PAGES_PER_BATCH]
                            for i in range(0, len(page_images), PAGES_PER_BATCH)]
            batch_count  = len(batches)

            BATCH_CONCURRENCY = 5
            sem       = asyncio.Semaphore(BATCH_CONCURRENCY)
            completed = [0]

            async def run_batch(bi: int, batch: list):
                async with sem:
                    batch_rule_hits = {
                        str(pg["page"]): rule_hits_by_page[str(pg["page"])]
                        for pg in batch
                        if str(pg["page"]) in rule_hits_by_page
                    }
                    try:
                        items = await loop.run_in_executor(
                            _executor,
                            self.judge.judge_image_batch,
                            batch, logo_b64, company_dict,
                            batch_rule_hits or None,
                        )
                        logger.info(f"[{job_id}] Vision 배치 {bi+1}/{batch_count}: {len(items)}건")
                        return items
                    except Exception as e:
                        logger.error(f"[{job_id}] Vision 배치 {bi+1} 오류: {e}")
                        return []
                    finally:
                        completed[0] += 1
                        pct = 65 + int((completed[0] / batch_count) * 25)
                        prog(pct, f"Claude Vision 분석 중 ({completed[0]}/{batch_count} 배치)…")

            vision_tasks   = [run_batch(bi, b) for bi, b in enumerate(batches)]
            vision_results = await asyncio.gather(*vision_tasks)
            for items in vision_results:
                all_vision_items.extend(items)

            logger.info(f"[{job_id}] Claude Vision 완료: {len(all_vision_items)}건")

        prog(90, "결과 합산 중…")

        # ── 7. 규칙 + Vision 합산 ──────────────────────────────
        # 페이지별 크롭 이미지 여부 맵 생성 (Vision 단독 탐지 결과 강등용)
        page_has_cropped: dict[int, bool] = {}
        for i in range(total):
            for j, img_d in enumerate(slide_images.get(i, [])):
                if img_d.get("is_cropped", False):
                    page_has_cropped[i + 1] = True
                    break

        merged = _merge_results(rule_hits_by_page, all_vision_items, total, page_has_cropped)

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
            claude_on=claude_on, ocr_on=ocr_on)

        prog(100, "검증 완료")
        logger.info(
            f"[{job_id}] 완료 {elapsed}s | "
            f"위반:{report['violation_count']} 주의:{report['caution_count']} | "
            f"모드: {' + '.join(mode_desc)}"
        )
        return report


# ── 결과 합산 (server_pipeline과 동일) ──────────────────────────
def _merge_results(rule_hits_by_page: dict, vision_items: list, total_slides: int,
                   page_has_cropped: dict = None) -> dict:
    """
    page_has_cropped: {page_number(1-based): True} — 해당 페이지에 크롭 이미지가 있음
    Vision AI가 위반으로 탐지해도 크롭 이미지가 있는 페이지의 로고/업체명은 주의로 강등
    """
    page_map: dict[int, list] = {}
    _cropped_pages = page_has_cropped or {}

    # allowed_terms 전체 목록 로드 (Vision 탐지 결과 허용 처리용)
    from core.config import load_dict as _load_dict
    _dict = _load_dict()
    _allowed_flat: set[str] = set()
    for _subcat_vals in _dict.get("allowed_terms", {}).values():
        for _t in _subcat_vals:
            if _t:
                _allowed_flat.add(_t.strip().lower())
    # 공공기관 등 고정 허용 기관명 키워드 (allowed_terms에 없어도 기본 허용)
    _FIXED_ALLOWED_ORGS = {
        "국민건강보험", "국민건강보험공단", "국민연금", "국민연금공단",
        "건강보험심사평가원", "국가철도공단", "한국도로공사", "한국토지주택공사",
        "국토교통부", "행정안전부", "과학기술정보통신부", "교육부", "고용노동부",
        "보건복지부", "환경부", "문화체육관광부", "농림축산식품부", "산업통상자원부",
        "중소벤처기업부", "국방부", "경찰청", "소방청", "기상청", "통계청",
        "조달청", "특허청", "식품의약품안전처", "금융위원회", "공정거래위원회",
        "한국전력공사", "한국수자원공사", "한국농어촌공사", "한국가스공사",
        "한국철도공사", "코레일", "한국공항공사", "인천국제공항공사",
        "NIA", "NIPA", "KISA", "ETRI", "KAIST", "KIST",
        "정부24", "민원24", "나라장터", "디지털서비스",
    }

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
        # Claude가 rule_hits 힌트를 받아서 구체적인 이름 대신 요약 문구를 반환하는 경우 제외
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
        # content에 특정 키워드가 포함된 더미 패턴도 제거
        _DUMMY_KEYWORDS = ("텍스트 추출로 탐지된", "사전 등록 실명 목록", "텍스트 추출로 사전 등록")
        _content_strip = content.strip()
        if (dtype in _DUMMY_TYPES
                or _content_strip in _DUMMY_CONTENTS
                or any(kw in _content_strip for kw in _DUMMY_KEYWORDS)):
            continue

        already = any(
            d["detected_text"].lower() == content.lower() and d["detection_type"] == dtype
            for d in page_map.get(p, [])
        )
        if already:
            continue

        # ── 일반 명사 체크: _COMMON_WORDS에 해당하면 허용으로 처리 (skip 아님, 결과에 표시)
        from services.rule_detector import _COMMON_WORDS as _CW
        import re as _re
        content_stripped = content.strip()
        # 인력명/이름 관련 모든 type에 대해 _COMMON_WORDS 체크
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
                "cropped":         False,
            })
            continue

        # 2글자 이하 인력명: 허용 기관명 포함 여부로 오탐 필터
        # Vision AI가 '국민' 검출 시 reason에 '국민건강보험공단' 같은 기관명이 언급된 경우 허용으로 처리
        if len(content_stripped) <= 2 and (dtype in ("참여인력명", "인력명", "업체명", "대표자명") or "인력" in dtype or "이름" in dtype):
            reason_text = it.get("reason", "") + " " + it.get("recommendation", "")
            _ORG_KEYWORDS = (
                r'공단|공사|위원회|연구원|연구소|진흥원|협회|학회|재단|센터|기관|'
                r'건강보험|국민연금|서민금융|대국민|안전처|안전부'
            )
            if _re.search(_ORG_KEYWORDS, reason_text):
                # skip 대신 허용으로 결과에 추가
                page_map.setdefault(p, []).append({
                    "detection_type":  dtype,
                    "detected_text":   content,
                    "verdict":         "허용",
                    "reason":          f"맥락상 기관명의 일부로 판단 – 인명 오탐 제외 (reason: {reason_text[:60]})",
                    "recommendation":  "기관명 복합어로 확인됨, 검증 불필요",
                    "confidence":      0.97,
                    "source":          "vision",
                    "cropped":         False,
                })
                continue

        # 크롭 이미지가 있는 페이지의 Vision 탐지 → 로고/업체명 위반 → 주의 강등
        vision_verdict = it.get("judgment", "주의")
        vision_cropped = False
        if _cropped_pages.get(p) and vision_verdict == "위반" and any(
            kw in dtype for kw in ("로고", "업체", "회사", "브랜드")
        ):
            vision_verdict = "주의"
            vision_cropped = True

        page_map.setdefault(p, []).append({
            "detection_type":  dtype,
            "detected_text":   content,
            "verdict":         vision_verdict,
            "reason":          it.get("reason", "") + (" [크롭 영역 밖 – 화면에 보이지 않음]" if vision_cropped else ""),
            "recommendation":  "크롭된 이미지의 숨겨진 영역에서 검출 – 직접 노출 아님, 이미지 재편집 권장" if vision_cropped else it.get("recommendation", ""),
            "confidence":      0.9,
            "source":          "vision",
            "cropped":         vision_cropped,
        })

    for page_str, hits in rule_hits_by_page.items():
        try:
            p = int(page_str)
        except ValueError:
            continue
        for h in hits:
            content = h.get("content", "").lower()
            # vision과 같은 텍스트가 있으면 source를 rule+vision으로 업데이트
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
                rule_src = h.get("source", "rule")  # 'rule' or 'ocr'
                if existing_src == "vision":
                    existing["source"] = f"{rule_src}+vision"
                # cropped=True 인 rule 항목이면 Vision 판정도 주의로 강등
                # (화면에 보이지 않는 크롭 영역 → 위반으로 처리 불가)
                if h.get("cropped", False):
                    weight_e = {"위반": 2, "주의": 1, "허용": 0}
                    if weight_e.get(existing["verdict"], 0) > 1:  # 위반이면 주의로 내림
                        existing["verdict"] = "주의"
                        existing["cropped"] = True
                        existing["reason"] = (existing.get("reason") or "") + " [크롭 영역 밖 – 화면에 보이지 않음]"
                        existing["recommendation"] = "크롭된 이미지의 숨겨진 영역에서 검출 – 직접 노출 아님, 이미지 재편집 권장"
                else:
                    # 판정은 더 강한 쪽으로
                    weight = {"위반": 2, "주의": 1, "허용": 0}
                    if weight.get(h.get("judgment", "주의"), 0) > weight.get(existing["verdict"], 0):
                        existing["verdict"] = h.get("judgment", "주의")
            else:
                # 새 항목 추가
                page_map.setdefault(p, []).append({
                    "detection_type":  h.get("type", "기타"),
                    "detected_text":   h.get("content", ""),
                    "verdict":         h.get("judgment", "주의"),
                    "reason":          h.get("reason", ""),
                    "recommendation":  h.get("recommendation", ""),
                    "confidence":      h.get("confidence", 0.95),
                    "source":          h.get("source", "rule"),
                    "cropped":         h.get("cropped", False),
                })

    return page_map


def _build_report(job_id, filename, total_slides, page_map, elapsed,
                  claude_on=False, ocr_on=True) -> dict:
    page_results = []
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

    vc_total = sum(p["violation_count"] for p in page_results)
    cc_total = sum(p["caution_count"]   for p in page_results)
    ac_total = sum(p["allowed_count"]   for p in page_results)

    if vc_total >= 5:                    risk = "HIGH"
    elif vc_total >= 1 or cc_total >= 5: risk = "MEDIUM"
    else:                                risk = "LOW"

    all_dets = [d for pr in page_results for d in pr["detections"]]

    def has_type(t):
        return any(t in d["detection_type"] and d["verdict"] == "위반" for d in all_dets)

    notes = []
    notes.append("업체명 직접 노출 있음"  if has_type("업체") or has_type("회사") else "명확한 업체명 노출 없음")
    notes.append("참여인력 실명 노출 있음" if has_type("인력") or has_type("대표") else "참여인력 실명 없음")
    notes.append("이메일/URL 노출 있음"   if has_type("이메일") or has_type("URL") else "이메일/URL 없음")
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
                "cropped":        d.get("cropped", False),
            })

    return {
        "job_id":                  job_id,
        "filename":                filename,
        "total_pages":             total_slides,
        "page_count":              total_slides,
        "processing_time_seconds": elapsed,
        "elapsed_sec":             elapsed,
        "created_at":              datetime.now().isoformat(),
        "risk_level":              risk,
        "violation_count":         vc_total,
        "caution_count":           cc_total,
        "allowed_count":           ac_total,
        "_analysis_mode":          analysis_mode,
        "_file_type":              "pptx",
        "items":                   flat_items,
        "summary_notes":           notes,
        "page_results":            page_results,
        "summary": {
            "no_company_name": not (has_type("업체") or has_type("회사")),
            "no_personnel":    not (has_type("인력") or has_type("대표")),
            "no_email_url":    not (has_type("이메일") or has_type("URL")),
            "indirect_count":  cc_total,
            "logo_detected":   any("로고" in d["detection_type"] and d["verdict"] != "허용"
                                   for d in all_dets),
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
