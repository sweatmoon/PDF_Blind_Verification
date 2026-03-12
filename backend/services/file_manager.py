"""
보안 파일 관리 – 즉시 삭제 / TTL 자동 삭제 / 고아 파일 정리
"""
import asyncio, shutil, time
from pathlib import Path
from core.config import get_logger, TMP_DIR, AUTO_DELETE_MIN

logger = get_logger("file_manager")

# TTL 레지스트리: job_id → 만료 timestamp
_ttl: dict[str, float] = {}


# ── 등록 ──────────────────────────────────────────────────────
def register_ttl(job_id: str, minutes: int | None = None):
    _ttl[job_id] = time.time() + (minutes or AUTO_DELETE_MIN) * 60
    logger.info(f"TTL 등록: {job_id} ({minutes or AUTO_DELETE_MIN}분)")


# ── 보안 삭제 ─────────────────────────────────────────────────
def _wipe_file(p: Path):
    """파일 내용을 0으로 덮어쓴 뒤 삭제"""
    try:
        size = p.stat().st_size
        if 0 < size < 50 * 1024 * 1024:          # 50 MB 이하만 덮어쓰기
            with open(p, "wb") as f:
                f.write(b"\x00" * min(size, 8192))
    except Exception:
        pass
    p.unlink(missing_ok=True)


def delete_job_files(job_id: str) -> bool:
    """job 관련 tmp 디렉토리 전체 삭제"""
    job_dir = TMP_DIR / job_id
    if not job_dir.exists():
        _ttl.pop(job_id, None)
        return True
    try:
        for f in job_dir.rglob("*"):
            if f.is_file():
                _wipe_file(f)
        shutil.rmtree(str(job_dir), ignore_errors=True)
        _ttl.pop(job_id, None)
        logger.info(f"파일 삭제 완료: {job_id}")
        return True
    except Exception as e:
        logger.error(f"파일 삭제 실패 {job_id}: {e}")
        return False


# ── 스케줄러 ──────────────────────────────────────────────────
async def cleanup_scheduler():
    """5분마다 만료 파일 정리"""
    while True:
        await asyncio.sleep(300)
        try:
            now = time.time()
            expired = [jid for jid, exp in list(_ttl.items()) if now > exp]
            for jid in expired:
                logger.info(f"TTL 만료 삭제: {jid}")
                delete_job_files(jid)
            # 고아 디렉토리 (2시간 초과)
            if TMP_DIR.exists():
                for d in TMP_DIR.iterdir():
                    if d.is_dir() and (time.time() - d.stat().st_mtime) > 7200:
                        logger.info(f"고아 디렉토리 삭제: {d}")
                        shutil.rmtree(str(d), ignore_errors=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"스케줄러 오류: {e}")


# ── 유효성 검사 ───────────────────────────────────────────────
def validate_pdf(path: Path, max_mb: int = 100) -> tuple[bool, str]:
    if not path.exists():
        return False, "파일 없음"
    mb = path.stat().st_size / 1024 / 1024
    if mb == 0:
        return False, "빈 파일"
    if mb > max_mb:
        return False, f"파일 크기 초과 ({mb:.1f} MB / 최대 {max_mb} MB)"
    with open(path, "rb") as f:
        if not f.read(5).startswith(b"%PDF-"):
            return False, "PDF 형식 오류"
    return True, "OK"


def storage_stats() -> dict:
    files, size = 0, 0
    if TMP_DIR.exists():
        for f in TMP_DIR.rglob("*"):
            if f.is_file():
                files += 1; size += f.stat().st_size
    return {"pending_jobs": len(_ttl), "tmp_files": files,
            "tmp_size_mb": round(size / 1024 / 1024, 2)}
