"""
PDF 파싱 서비스 – PyMuPDF 기반
- 텍스트 추출 (페이지별 + 단어 bbox)
- 페이지 이미지 렌더링 (썸네일, OCR용)
- 내장 이미지 추출
- 메타데이터 추출
- 스캔 PDF 판별
"""
from __future__ import annotations
import base64, io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import fitz                    # PyMuPDF
from PIL import Image

from core.config import get_logger

logger = get_logger("pdf_service")


@dataclass
class PageData:
    page_number: int           # 1-based
    text:        str           = ""
    words:       list          = field(default_factory=list)  # (x0,y0,x1,y1,word,…)
    images:      list          = field(default_factory=list)  # image dicts
    page_w:      float         = 0.0   # pt
    page_h:      float         = 0.0   # pt


class PDFService:
    def __init__(self, path: Path):
        self.path       = path
        self._doc       = None
        self.total_pages = 0
        self.metadata:  dict = {}
        self.is_scanned: bool = False

    # ── 열기 ─────────────────────────────────────────────────
    def open(self) -> bool:
        try:
            self._doc = fitz.open(str(self.path))
            self.total_pages = len(self._doc)
            self.metadata    = self._extract_metadata()
            self.is_scanned  = self._detect_scan()
            logger.info(f"PDF 열기: {self.total_pages}p, scanned={self.is_scanned}")
            return True
        except Exception as e:
            logger.error(f"PDF 열기 실패: {e}")
            return False

    def close(self):
        if self._doc:
            try: self._doc.close()
            except Exception: pass
            self._doc = None

    def __enter__(self):  self.open();  return self
    def __exit__(self, *_): self.close()

    # ── 메타데이터 ────────────────────────────────────────────
    def _extract_metadata(self) -> dict:
        out = {}
        try:
            for k, v in (self._doc.metadata or {}).items():
                if v and str(v).strip():
                    out[k] = str(v).strip()
        except Exception: pass
        return out

    def _detect_scan(self) -> bool:
        """텍스트 비율이 30% 미만이면 스캔본으로 판단"""
        text_pages = sum(
            1 for i in range(min(self.total_pages, 10))
            if len(self._doc[i].get_text("text").strip()) > 50
        )
        return text_pages < max(1, min(self.total_pages, 10)) * 0.3

    # ── 페이지 추출 ───────────────────────────────────────────
    def extract_page(self, idx: int) -> PageData:
        """0-based index"""
        pg   = self._doc[idx]
        text = pg.get_text("text")
        words = pg.get_text("words")          # (x0,y0,x1,y1,word,block,line,wi)
        rect  = pg.rect

        images = []
        for img in pg.get_images(full=True):
            xref = img[0]
            try:
                bi = self._doc.extract_image(xref)
                if bi:
                    images.append({
                        "data": bi.get("image", b""),
                        "ext":  bi.get("ext", "png"),
                        "w":    bi.get("width", 0),
                        "h":    bi.get("height", 0),
                    })
            except Exception: pass

        return PageData(
            page_number=idx + 1,
            text=text, words=words, images=images,
            page_w=rect.width, page_h=rect.height,
        )

    # ── 썸네일 렌더링 (base64 JPEG) ───────────────────────────
    def thumbnail_b64(self, idx: int, max_w: int = 700) -> str:
        try:
            pg    = self._doc[idx]
            scale = min(max_w / max(pg.rect.width, 1), 2.0)
            pix   = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return base64.b64encode(pix.tobytes("jpeg")).decode()
        except Exception as e:
            logger.warning(f"썸네일 실패 p{idx+1}: {e}"); return ""

    # ── OCR용 PIL Image ───────────────────────────────────────
    def render_for_ocr(self, idx: int, dpi: int = 200) -> Optional[Image.Image]:
        try:
            pg  = self._doc[idx]
            s   = dpi / 72.0
            pix = pg.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
            return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        except Exception as e:
            logger.warning(f"OCR 렌더 실패: {e}"); return None
