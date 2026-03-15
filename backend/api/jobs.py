"""
대시보드용 Jobs API
- 업로드 & 서버사이드 파이프라인 실행
- 작업 목록/상태/진행률 조회
- SSE 실시간 진행률 스트리밍
- 결과 조회 & 삭제
- 동시 처리 1개 제한 + 큐잉 (옵션 A)
"""
from __future__ import annotations
import asyncio, json
from collections import deque
from datetime import datetime
from core.config import now_kst_iso
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from models.schemas import JobStatus
from services.file_manager import validate_pdf, register_ttl, delete_job_files
from services.server_pipeline import get_server_pipeline
from services.ppt_pipeline import get_ppt_pipeline
from core.config import (
    get_logger, sanitize_filename, generate_job_id,
    get_job_tmp_dir, get_job, set_job, update_job, delete_job, list_jobs,
    MAX_FILE_SIZE_MB,
)

router = APIRouter()
logger = get_logger("jobs_api")

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB

# ── 동시 처리 제한 (옵션 A: 동시 1개 + 큐잉) ──────────────────
_pipeline_sem  = asyncio.Semaphore(1)   # 동시 실행 최대 1개
_job_queue: deque[tuple] = deque()      # (job_id, path, filename, file_type)
_queue_lock    = asyncio.Lock()
_queue_runner_started = False


def _max_bytes() -> int:
    from core.config import MAX_FILE_SIZE_MB as _MB
    return _MB * 1024 * 1024


ALLOWED_EXTENSIONS = {".pdf", ".pptx"}
PPTX_MAGIC = b"PK"   # ZIP 기반 포맷 (OOXML)


def _detect_file_type(fname: str, first_chunk: bytes) -> str:
    """파일명 + 매직바이트로 파일 타입 감지. 'pdf' 또는 'pptx' 반환"""
    ext = Path(fname).suffix.lower()
    if ext == ".pdf" and first_chunk[:5].startswith(b"%PDF-"):
        return "pdf"
    if ext == ".pptx" and first_chunk[:2] == PPTX_MAGIC:
        return "pptx"
    return ""


# ── 업로드 & 서버사이드 검증 시작 ────────────────────────────────
@router.post("/upload")
async def dashboard_upload(request: Request,
                            file: UploadFile = File(...)):
    fname = (file.filename or "document.pdf").strip()
    ext   = Path(fname).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "PDF 또는 PPTX 파일만 업로드 가능합니다.")

    safe_name = sanitize_filename(fname)
    job_id    = generate_job_id()
    job_dir   = get_job_tmp_dir(job_id)
    dest      = job_dir / safe_name

    # 스트리밍으로 바로 파일에 기록 (메모리에 전체 로드 X)
    first_chunk_data = b""
    first_chunk_done = False
    file_type        = ""
    total_size       = 0
    max_bytes        = _max_bytes()
    try:
        with open(dest, "wb") as fp:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_bytes:
                    fp.close()
                    dest.unlink(missing_ok=True)
                    from core.config import MAX_FILE_SIZE_MB as _MB
                    raise HTTPException(413, f"파일 크기 초과 (최대 {_MB}MB)")
                # 첫 청크에서 파일 타입 검사
                if not first_chunk_done:
                    first_chunk_data = chunk[:16]
                    file_type = _detect_file_type(fname, first_chunk_data)
                    if not file_type:
                        fp.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(400, "유효하지 않은 파일입니다. (PDF 또는 PPTX만 허용)")
                    first_chunk_done = True
                fp.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"파일 읽기 오류: {e}")

    if total_size < 100:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "파일이 너무 작거나 비어 있습니다.")

    set_job(job_id, {
        "job_id":     job_id,
        "mode":       "server",
        "file_type":  file_type,   # 'pdf' or 'pptx'
        "status":     JobStatus.PENDING.value,
        "progress":   0,
        "message":    "검증 대기 중…",
        "filename":   fname,
        "safe_name":  safe_name,
        "file_size":  total_size,
        "created_at": now_kst_iso(),
        "report":     None,
        "error":      None,
    })
    register_ttl(job_id)

    # 큐에 추가 후 큐 워커 시작
    queue_pos = await _enqueue_job(job_id, dest, fname, file_type)

    logger.info(f"대시보드 업로드: {safe_name} ({total_size/1024:.1f} KB) job={job_id} type={file_type} 큐위치={queue_pos}")
    msg = "검증을 시작합니다." if queue_pos == 0 else f"대기 중… (앞에 {queue_pos}개 작업 있음)"
    return JSONResponse({
        "job_id":     job_id,
        "status":     "pending",
        "filename":   fname,
        "file_type":  file_type,
        "queue_pos":  queue_pos,
        "message":    msg,
    })


# ── 큐 관리 ────────────────────────────────────────────────────
async def _enqueue_job(job_id: str, path: Path, filename: str, file_type: str) -> int:
    """큐에 job 추가 후 워커 시작. 현재 큐 위치(0=즉시 실행) 반환."""
    global _queue_runner_started
    async with _queue_lock:
        _job_queue.append((job_id, path, filename, file_type))
        pos = len(_job_queue) - 1
        if not _queue_runner_started:
            _queue_runner_started = True
            asyncio.ensure_future(_queue_worker())
    return pos


async def _queue_worker():
    """큐에서 job을 하나씩 꺼내 순차 실행. 세마포어로 동시 1개 보장."""
    global _queue_runner_started
    while True:
        async with _queue_lock:
            if not _job_queue:
                _queue_runner_started = False
                return
            job_id, path, filename, file_type = _job_queue.popleft()
            # 남은 대기 job들 메시지 갱신
            for i, (jid, *_) in enumerate(_job_queue):
                update_job(jid, message=f"대기 중… (앞에 {i+1}개 작업 처리 중)")

        async with _pipeline_sem:
            if file_type == "pptx":
                await _run_ppt_pipeline(job_id, path, filename)
            else:
                await _run_server_pipeline(job_id, path, filename)


# ── 백그라운드 실행 (PDF) ───────────────────────────────────────
async def _run_server_pipeline(job_id: str, path: Path, filename: str):
    update_job(job_id, status=JobStatus.PROCESSING.value, message="검증 시작…")
    try:
        pipeline = get_server_pipeline()
        report   = await pipeline.run(job_id, path, filename)
        update_job(job_id,
                   status=JobStatus.COMPLETED.value,
                   progress=100,
                   message="검증 완료",
                   page_count=report.get("page_count") if report else None,
                   report=report)
    except Exception as e:
        logger.error(f"[{job_id}] PDF 파이프라인 오류: {e}", exc_info=True)
        update_job(job_id,
                   status=JobStatus.FAILED.value,
                   progress=0,
                   message=f"검증 실패: {str(e)[:100]}",
                   error=str(e))
        try:
            delete_job_files(job_id)
        except Exception:
            pass


# ── 백그라운드 실행 (PPTX) ──────────────────────────────────────
async def _run_ppt_pipeline(job_id: str, path: Path, filename: str):
    update_job(job_id, status=JobStatus.PROCESSING.value, message="PPTX 검증 시작…")
    try:
        pipeline = get_ppt_pipeline()
        report   = await pipeline.run(job_id, path, filename)
        update_job(job_id,
                   status=JobStatus.COMPLETED.value,
                   progress=100,
                   message="검증 완료",
                   page_count=report.get("page_count") if report else None,
                   report=report)
    except Exception as e:
        logger.error(f"[{job_id}] PPT 파이프라인 오류: {e}", exc_info=True)
        update_job(job_id,
                   status=JobStatus.FAILED.value,
                   progress=0,
                   message=f"검증 실패: {str(e)[:100]}",
                   error=str(e))
        try:
            delete_job_files(job_id)
        except Exception:
            pass


# ── 전체 Job 목록 ─────────────────────────────────────────────
@router.get("/list")
def job_list():
    """
    대시보드용 작업 목록.
    - 진행 중: 최신순
    - 완료/실패: 최신순 스택
    report 필드는 제외하고 메타 정보만 반환
    """
    jobs = list_jobs()
    # server 모드 job만 (또는 전체)
    result = []
    for j in jobs:
        result.append({
            "job_id":     j.get("job_id"),
            "mode":       j.get("mode", "server"),
            "status":     j.get("status"),
            "progress":   j.get("progress", 0),
            "message":    j.get("message", ""),
            "filename":   j.get("filename", ""),
            "file_size":  j.get("file_size", 0),
            "created_at": j.get("created_at", ""),
            "has_report": j.get("has_report") or j.get("report") is not None,
            "risk_level": (j.get("report") or {}).get("risk_level"),
            "violation_count": (j.get("report") or {}).get("violation_count"),
            "caution_count":   (j.get("report") or {}).get("caution_count"),
            "page_count":      (j.get("report") or {}).get("page_count") or j.get("page_count"),
            "error":      j.get("error"),
        })
    # 최신순 정렬
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return JSONResponse(result)


# ── 단일 Job 상태 ─────────────────────────────────────────────
@router.get("/{job_id}/status")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return JSONResponse({
        "job_id":    job_id,
        "status":    job.get("status"),
        "progress":  job.get("progress", 0),
        "message":   job.get("message", ""),
        "has_report": job.get("has_report") or job.get("report") is not None,
        "risk_level": (job.get("report") or {}).get("risk_level"),
        "violation_count": (job.get("report") or {}).get("violation_count"),
        "caution_count":   (job.get("report") or {}).get("caution_count"),
        "error":     job.get("error"),
    })


# ── SSE 실시간 진행률 ─────────────────────────────────────────
@router.get("/{job_id}/progress")
async def job_progress_sse(job_id: str, request: Request):
    """Server-Sent Events로 실시간 진행률 스트리밍"""
    async def event_generator():
        last_pct = -1
        timeout  = 0
        while True:
            if await request.is_disconnected():
                break
            job = get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'not_found'})}\n\n"
                break
            pct  = job.get("progress", 0)
            msg  = job.get("message", "")
            stat = job.get("status", "")
            if pct != last_pct:
                last_pct = pct
                timeout  = 0
                payload = json.dumps({
                    "progress": pct, "message": msg, "status": stat,
                    "risk_level": (job.get("report") or {}).get("risk_level"),
                    "violation_count": (job.get("report") or {}).get("violation_count"),
                    "caution_count":   (job.get("report") or {}).get("caution_count"),
                }, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            if stat in ("completed", "failed"):
                break
            await asyncio.sleep(1.5)
            timeout += 1
            if timeout > 400:   # 10분 타임아웃
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── 리포트 조회 ───────────────────────────────────────────────
@router.get("/{job_id}/report")
def job_report(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    if job.get("status") != "completed":
        raise HTTPException(400, f"아직 완료되지 않은 작업입니다. (상태: {job.get('status')})")
    report = job.get("report")
    if not report:
        raise HTTPException(404, "리포트 데이터가 없습니다.")
    return JSONResponse(report)


# ── 작업 삭제 ─────────────────────────────────────────────────
@router.delete("/{job_id}")
def job_delete(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    try:
        delete_job_files(job_id)
    except Exception:
        pass
    delete_job(job_id)
    return JSONResponse({"success": True, "message": "작업이 삭제되었습니다."})


# ── 전체 작업 삭제 ────────────────────────────────────────────
@router.delete("")
def job_delete_all():
    """완료/실패 상태의 모든 작업을 삭제합니다."""
    jobs = list_jobs()
    deleted = 0
    for j in jobs:
        jid = j.get("job_id")
        if not jid:
            continue
        # 진행 중(pending/processing)은 삭제 제외
        if j.get("status") in ("pending", "processing"):
            continue
        try:
            delete_job_files(jid)
        except Exception:
            pass
        delete_job(jid)
        deleted += 1
    return JSONResponse({"success": True, "deleted": deleted, "message": f"{deleted}개 작업이 삭제되었습니다."})
