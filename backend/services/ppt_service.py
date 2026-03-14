"""
PPT(X) 파싱 서비스 – python-pptx 기반
- 텍스트 추출: 텍스트박스, 표, 도형, 슬라이드 노트, 숨겨진 슬라이드, 하이퍼링크
- 슬라이드 이미지 렌더링: 내장 이미지 추출 (OCR / Vision AI 입력용)
- 메타데이터 추출
"""
from __future__ import annotations
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

from core.config import get_logger

logger = get_logger("ppt_service")


def _iter_group_shapes(group_shape):
    """그룹 도형을 재귀적으로 순회해 모든 하위 shape 반환 (중첩 그룹 지원)"""
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        for child in group_shape.shapes:
            if child.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from _iter_group_shapes(child)  # 재귀
            else:
                yield child
    except Exception:
        pass


@dataclass
class SlideData:
    slide_number: int          # 1-based
    text:         str  = ""    # 모든 텍스트 합산
    text_items:   list = field(default_factory=list)  # [(source, text), ...]
    images:       list = field(default_factory=list)  # image dicts
    hyperlinks:   list = field(default_factory=list)  # [url, ...]
    is_hidden:    bool = False


class PPTService:
    def __init__(self, path: Path):
        self.path         = path
        self._prs         = None
        self.total_slides = 0
        self.metadata:    dict = {}
        self.is_pptx:     bool = True

    # ── 열기 ─────────────────────────────────────────────────
    def open(self) -> bool:
        try:
            from pptx import Presentation
            self._prs         = Presentation(str(self.path))
            self.total_slides = len(self._prs.slides)
            self.metadata     = self._extract_metadata()
            logger.info(f"PPTX 열기: {self.total_slides}슬라이드")
            return True
        except Exception as e:
            logger.error(f"PPTX 열기 실패: {e}")
            return False

    def close(self):
        self._prs = None

    def __enter__(self):  self.open();  return self
    def __exit__(self, *_): self.close()

    # ── 메타데이터 ────────────────────────────────────────────
    def _extract_metadata(self) -> dict:
        out = {}
        try:
            cp = self._prs.core_properties
            fields = [
                ("author",           cp.author),
                ("last_modified_by", cp.last_modified_by),
                ("company",          getattr(cp, "company", "")),
                ("title",            cp.title),
                ("subject",          cp.subject),
                ("keywords",         cp.keywords),
            ]
            for k, v in fields:
                if v and str(v).strip():
                    out[k] = str(v).strip()
        except Exception as e:
            logger.warning(f"메타데이터 추출 실패: {e}")
        return out

    # ── 슬라이드 추출 ─────────────────────────────────────────
    def extract_slide(self, idx: int) -> SlideData:
        """0-based index"""
        slide      = self._prs.slides[idx]
        slide_num  = idx + 1
        text_items = []
        images     = []
        hyperlinks = []

        # 숨겨진 슬라이드 여부
        is_hidden = False
        try:
            from pptx.oxml.ns import qn
            show = slide._element.get("show")
            is_hidden = (show == "0")
        except Exception:
            pass

        # ── 모든 shape 순회 ──────────────────────────────────
        for shape in slide.shapes:
            # 텍스트박스 / 도형
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    # ★ para.text 우선 사용: runs가 없어도 전체 텍스트 반환
                    #   (runs만 순회하면 runs=[] 단락의 텍스트가 완전 누락됨)
                    line = para.text or ""

                    # 하이퍼링크는 runs에서만 추출 가능
                    for run in para.runs:
                        try:
                            if run.hyperlink and run.hyperlink.address:
                                hyperlinks.append(run.hyperlink.address)
                        except Exception:
                            pass

                    if line.strip():
                        src = "note" if shape.shape_type == 13 else "textbox"
                        text_items.append((src, line.strip()))

            # 표(Table)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        ct = cell.text.strip()
                        if ct:
                            text_items.append(("table", ct))
                            # 셀 하이퍼링크
                            try:
                                for para in cell.text_frame.paragraphs:
                                    for run in para.runs:
                                        if run.hyperlink and run.hyperlink.address:
                                            hyperlinks.append(run.hyperlink.address)
                            except Exception:
                                pass

            # 이미지 추출 (Picture shape)
            try:
                from pptx.enum.shapes import MSO_SHAPE_TYPE
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    img_blob = shape.image.blob
                    img_ext  = shape.image.ext          # "jpeg", "png", …
                    pil_img  = Image.open(io.BytesIO(img_blob))
                    w, h     = pil_img.size
                    images.append({
                        "data": img_blob,
                        "ext":  img_ext,
                        "w":    w,
                        "h":    h,
                        "pil":  pil_img,
                    })
            except Exception:
                pass

            # 그룹 shape 안 텍스트 + 이미지 재귀 처리
            try:
                from pptx.enum.shapes import MSO_SHAPE_TYPE
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    for child in _iter_group_shapes(shape):
                        # 텍스트박스
                        if child.has_text_frame:
                            for para in child.text_frame.paragraphs:
                                line = para.text or ""
                                for run in para.runs:
                                    try:
                                        if run.hyperlink and run.hyperlink.address:
                                            hyperlinks.append(run.hyperlink.address)
                                    except Exception:
                                        pass
                                if line.strip():
                                    text_items.append(("textbox", line.strip()))
                        # 이미지
                        if child.shape_type == MSO_SHAPE_TYPE.PICTURE:
                            try:
                                img_blob = child.image.blob
                                img_ext  = child.image.ext
                                pil_img  = Image.open(io.BytesIO(img_blob))
                                w, h     = pil_img.size
                                images.append({
                                    "data": img_blob,
                                    "ext":  img_ext,
                                    "w":    w,
                                    "h":    h,
                                    "pil":  pil_img,
                                })
                            except Exception:
                                pass
            except Exception:
                pass

        # ── 슬라이드 노트 ────────────────────────────────────
        try:
            if slide.has_notes_slide:
                notes_tf = slide.notes_slide.notes_text_frame
                for para in notes_tf.paragraphs:
                    nt = para.text.strip()
                    if nt:
                        text_items.append(("note", nt))
        except Exception:
            pass

        # 전체 텍스트 합산
        full_text = "\n".join(t for _, t in text_items)

        return SlideData(
            slide_number=slide_num,
            text=full_text,
            text_items=text_items,
            images=images,
            hyperlinks=list(set(hyperlinks)),
            is_hidden=is_hidden,
        )

    # ── 슬라이드 썸네일 (이미지 → PIL) ───────────────────────
    def slide_thumbnail(self, idx: int, max_w: int = 1000) -> Optional[Image.Image]:
        """
        슬라이드 전체를 하나의 이미지로 렌더링.
        LibreOffice 없이 python-pptx만으로는 불가하므로
        슬라이드 내 이미지들을 흰 캔버스에 합성하여 근사치 렌더링.
        → Claude Vision AI 입력용
        """
        try:
            slide_data = self.extract_slide(idx)
            if not slide_data.images:
                return None

            # 슬라이드 크기 (EMU → px, 96dpi 기준)
            prs = self._prs
            slide_w_px = int(prs.slide_width  / 914400 * 96)
            slide_h_px = int(prs.slide_height / 914400 * 96)

            # 스케일 조정
            scale = min(max_w / max(slide_w_px, 1), 1.5)
            canvas_w = int(slide_w_px * scale)
            canvas_h = int(slide_h_px * scale)

            canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))

            # 이미지들을 캔버스 크기에 맞게 배치 (단순 타일링)
            slide      = prs.slides[idx]
            img_shapes = []
            try:
                from pptx.enum.shapes import MSO_SHAPE_TYPE
                for shape in slide.shapes:
                    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                        img_shapes.append(shape)
                    elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                        for child in shape.shapes:
                            if child.shape_type == MSO_SHAPE_TYPE.PICTURE:
                                img_shapes.append(child)
            except Exception:
                pass

            for shape in img_shapes:
                try:
                    left = int(shape.left   / 914400 * 96 * scale)
                    top  = int(shape.top    / 914400 * 96 * scale)
                    w    = int(shape.width  / 914400 * 96 * scale)
                    h    = int(shape.height / 914400 * 96 * scale)

                    pil = Image.open(io.BytesIO(shape.image.blob))
                    if pil.mode not in ("RGB", "RGBA"):
                        pil = pil.convert("RGB")
                    if w > 0 and h > 0:
                        pil = pil.resize((w, h), Image.LANCZOS)
                    canvas.paste(pil, (max(0, left), max(0, top)))
                except Exception:
                    pass

            return canvas
        except Exception as e:
            logger.warning(f"슬라이드 썸네일 실패 s{idx+1}: {e}")
            return None

    # ── 슬라이드 전체 이미지 렌더링 (Claude Vision 입력) ──────
    def render_for_vision(self, idx: int) -> Optional[Image.Image]:
        """Claude Vision AI 입력용 슬라이드 이미지"""
        return self.slide_thumbnail(idx, max_w=1200)
