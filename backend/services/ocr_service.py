"""
OCR 서비스 – Google Vision API 우선, Tesseract 폴백
- Google Vision: ~1.5초/페이지 (배치 16장 병렬)
- Tesseract: ~6~8초/페이지 (GV 키 없을 때만)
"""
from __future__ import annotations
import io, base64, json, threading
import urllib.request as _ur
import urllib.error as _ue
from typing import Optional
from PIL import Image
from core.config import get_logger, OCR_ENABLED, get_google_vision_key

logger = get_logger("ocr_service")

OCR_TIMEOUT_SECONDS = 30   # 페이지당 최대 시간 (Tesseract 폴백용)
GV_TIMEOUT_SECONDS  = 20   # Google Vision 배치 요청 타임아웃


class OCRService:
    def __init__(self):
        self.enabled = OCR_ENABLED
        self._tess = None

        if self.enabled:
            try:
                import pytesseract
                pytesseract.get_tesseract_version()
                self._tess = pytesseract
                logger.info("Tesseract OCR 준비 완료 (폴백용)")
            except Exception as e:
                logger.warning(f"Tesseract 없음: {e}")

    @property
    def gv_key(self) -> str:
        """런타임에 키를 읽어 항상 최신 값 반환"""
        return get_google_vision_key()

    @property
    def use_google_vision(self) -> bool:
        return bool(self.gv_key)

    # ── Google Vision: 단일 페이지 ────────────────────────────
    def gv_ocr_single(self, img: Image.Image) -> str:
        """Google Vision API로 단일 이미지 OCR"""
        key = self.gv_key
        if not key:
            return ""
        try:
            jpeg_bytes = self._img_to_jpeg(img)
            b64 = base64.b64encode(jpeg_bytes).decode()
            body = json.dumps({
                "requests": [{
                    "image": {"content": b64},
                    "features": [{"type": "TEXT_DETECTION"}]
                }]
            }).encode()
            req = _ur.Request(
                f"https://vision.googleapis.com/v1/images:annotate?key={key}",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with _ur.urlopen(req, timeout=GV_TIMEOUT_SECONDS) as resp:
                result = json.loads(resp.read())
            responses = result.get("responses", [{}])
            text = responses[0].get("fullTextAnnotation", {}).get("text", "")
            return text.strip()
        except Exception as e:
            logger.warning(f"Google Vision 단일 OCR 실패: {e}")
            return ""

    # ── Google Vision: 배치 (최대 16장) ──────────────────────
    def gv_ocr_batch(self, images: list[tuple[int, Image.Image]]) -> dict[int, str]:
        """
        Google Vision API 배치 요청
        images: [(page_idx, PIL.Image), ...]
        반환: {page_idx: text}
        """
        key = self.gv_key
        if not key or not images:
            return {}
        try:
            requests_payload = []
            for _, img in images:
                jpeg_bytes = self._img_to_jpeg(img)
                b64 = base64.b64encode(jpeg_bytes).decode()
                requests_payload.append({
                    "image": {"content": b64},
                    "features": [{"type": "TEXT_DETECTION"}]
                })
            body = json.dumps({"requests": requests_payload}).encode()
            req = _ur.Request(
                f"https://vision.googleapis.com/v1/images:annotate?key={key}",
                data=body,
                headers={"Content-Type": "application/json"}
            )
            with _ur.urlopen(req, timeout=GV_TIMEOUT_SECONDS) as resp:
                result = json.loads(resp.read())

            out = {}
            for (page_idx, _), response in zip(images, result.get("responses", [])):
                err = response.get("error")
                if err:
                    logger.warning(f"GV p{page_idx+1} 오류: {err}")
                    continue
                text = response.get("fullTextAnnotation", {}).get("text", "").strip()
                if text:
                    out[page_idx] = text
            return out
        except Exception as e:
            logger.warning(f"Google Vision 배치 OCR 실패: {e}")
            return {}

    # ── Tesseract 폴백 ────────────────────────────────────────
    def _tesseract_ocr(self, img: Image.Image) -> str:
        """Tesseract OCR (GV 키 없거나 GV 실패 시 폴백)"""
        if not self._tess:
            return ""
        try:
            img = self._preprocess(img)
            def _do():
                return self._tess.image_to_string(
                    img, lang="kor+eng", config="--oem 3 --psm 11").strip()
            return self._run_with_timeout(_do, timeout=OCR_TIMEOUT_SECONDS)
        except Exception as e:
            logger.debug(f"Tesseract 실패: {e}")
            return ""

    # ── 통합 단일 OCR (GV 우선) ───────────────────────────────
    def from_image(self, img: Image.Image, lang: str = "kor+eng") -> str:
        """단일 이미지 OCR: GV 우선, 폴백 Tesseract"""
        if not self.enabled:
            return ""
        if self.use_google_vision:
            text = self.gv_ocr_single(img)
            if text:
                return text
            # GV 실패 시 Tesseract 폴백
            logger.debug("GV 실패 → Tesseract 폴백")
        return self._tesseract_ocr(img)

    def from_bytes(self, data: bytes, lang: str = "kor+eng") -> str:
        if not self.enabled or not data:
            return ""
        try:
            img = Image.open(io.BytesIO(data))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            return self.from_image(img, lang)
        except Exception as e:
            logger.debug(f"OCR bytes 실패: {e}")
            return ""

    # ── 헬퍼 ─────────────────────────────────────────────────
    def _img_to_jpeg(self, img: Image.Image, quality: int = 85) -> bytes:
        """PIL Image → JPEG bytes (Google Vision 전송용)"""
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def _preprocess(self, img: Image.Image) -> Image.Image:
        """Tesseract용 전처리: 너무 작거나 큰 이미지 리사이즈"""
        try:
            if img.mode not in ("RGB", "L", "RGBA"):
                img = img.convert("RGB")
            if img.width < 300:
                ratio = 300 / img.width
                img = img.resize((300, int(img.height * ratio)), Image.LANCZOS)
            elif img.width > 1400:
                ratio = 1400 / img.width
                img = img.resize((1400, int(img.height * ratio)), Image.LANCZOS)
        except Exception:
            pass
        return img

    def _run_with_timeout(self, fn, *args, timeout: int = OCR_TIMEOUT_SECONDS) -> str:
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
            logger.warning(f"Tesseract 타임아웃 ({timeout}s) - 건너뜀")
            return ""
        if result["error"]:
            logger.debug(f"Tesseract 오류: {result['error']}")
            return ""
        return result["value"]


# 싱글톤
_inst: OCRService | None = None

def get_ocr() -> OCRService:
    global _inst
    if _inst is None:
        _inst = OCRService()
    return _inst
