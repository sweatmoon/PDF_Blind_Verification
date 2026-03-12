"""
OCR 서비스 – Tesseract (kor+eng)
"""
from __future__ import annotations
import io
from typing import Optional
from PIL import Image
from core.config import get_logger, OCR_ENABLED

logger = get_logger("ocr_service")


class OCRService:
    def __init__(self):
        self.enabled = OCR_ENABLED
        if self.enabled:
            try:
                import pytesseract
                pytesseract.get_tesseract_version()
                self._tess = pytesseract
                logger.info("Tesseract OCR 준비 완료")
            except Exception as e:
                logger.warning(f"Tesseract 없음 → OCR 비활성: {e}")
                self.enabled = False

    # ── 이미지 → 텍스트 ──────────────────────────────────────
    def from_image(self, img: Image.Image, lang: str = "kor+eng") -> str:
        if not self.enabled: return ""
        try:
            img = self._preprocess(img)
            return self._tess.image_to_string(img, lang=lang,
                                              config="--oem 3 --psm 6").strip()
        except Exception as e:
            logger.debug(f"OCR image 실패: {e}"); return ""

    def from_bytes(self, data: bytes, lang: str = "kor+eng") -> str:
        if not self.enabled or not data: return ""
        try:
            img = Image.open(io.BytesIO(data))
            if img.mode not in ("RGB", "L"): img = img.convert("RGB")
            return self.from_image(img, lang)
        except Exception as e:
            logger.debug(f"OCR bytes 실패: {e}"); return ""

    def _preprocess(self, img: Image.Image) -> Image.Image:
        try:
            if img.mode not in ("RGB", "L", "RGBA"): img = img.convert("RGB")
            if img.width < 300:
                ratio = 300 / img.width
                img = img.resize((300, int(img.height * ratio)), Image.LANCZOS)
        except Exception: pass
        return img


# 싱글톤
_inst: OCRService | None = None
def get_ocr() -> OCRService:
    global _inst
    if _inst is None: _inst = OCRService()
    return _inst
