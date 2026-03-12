"""
OCR 서비스 – Tesseract (kor+eng)
- 타임아웃 제한 추가 (페이지당 최대 20초)
- 스레드 기반 타임아웃으로 블로킹 방지
"""
from __future__ import annotations
import io, time, threading
from typing import Optional
from PIL import Image
from core.config import get_logger, OCR_ENABLED

logger = get_logger("ocr_service")

OCR_TIMEOUT_SECONDS = 20   # 페이지당 OCR 최대 시간


class OCRService:
    def __init__(self):
        self.enabled = OCR_ENABLED
        self._tess = None
        if self.enabled:
            try:
                import pytesseract
                pytesseract.get_tesseract_version()
                self._tess = pytesseract
                logger.info("Tesseract OCR 준비 완료")
            except Exception as e:
                logger.warning(f"Tesseract 없음 → OCR 비활성: {e}")
                self.enabled = False

    # ── 타임아웃 래퍼 ────────────────────────────────────────
    def _run_with_timeout(self, fn, *args, timeout: int = OCR_TIMEOUT_SECONDS) -> str:
        """스레드 기반 타임아웃으로 OCR 실행"""
        result = {"value": "", "error": None}

        def worker():
            try:
                result["value"] = fn(*args)
            except Exception as e:
                result["error"] = str(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            logger.warning(f"OCR 타임아웃 ({timeout}s) - 건너뜀")
            return ""
        if result["error"]:
            logger.debug(f"OCR 오류: {result['error']}")
            return ""
        return result["value"]

    # ── 이미지 → 텍스트 ──────────────────────────────────────
    def from_image(self, img: Image.Image, lang: str = "kor+eng") -> str:
        if not self.enabled: return ""
        try:
            img = self._preprocess(img)
            def _do():
                return self._tess.image_to_string(
                    img, lang=lang, config="--oem 3 --psm 6").strip()
            return self._run_with_timeout(_do, timeout=OCR_TIMEOUT_SECONDS)
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
            # 너무 작은 이미지만 업스케일 (큰 이미지는 그대로 - 속도 최적화)
            if img.width < 300:
                ratio = 300 / img.width
                img = img.resize((300, int(img.height * ratio)), Image.LANCZOS)
            # 너무 큰 이미지는 다운스케일 (메모리 및 속도 최적화)
            elif img.width > 2000:
                ratio = 2000 / img.width
                img = img.resize((2000, int(img.height * ratio)), Image.LANCZOS)
        except Exception: pass
        return img


# 싱글톤
_inst: OCRService | None = None
def get_ocr() -> OCRService:
    global _inst
    if _inst is None: _inst = OCRService()
    return _inst
