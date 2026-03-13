"""
대시보드용 Jobs API
- 업로드 & 서버사이드 파이프라인 실행
- 작업 목록/상태/진행률 조회
- SSE 실시간 진행률 스트리밍
- 결과 조회 & 삭제
"""
from __future__ import annotations
import asyncio, json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse, StreamingResponse

from models.schemas import JobStatus
from services.file_manager import validate_pdf, register_ttl, delete_job_files
from services.server_pipeline import get_server_pipeline
from core.config import (
    get_logger, sanitize_filename, generate_job_id,
    get_job_tmp_dir, get_job, set_job, update_job, delete_job, list_jobs,
    MAX_FILE_SIZE_MB,
)

router = APIRouter()
logger = get_logger("jobs_api")

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB (큰 청크로 루프 횟수 줄임)


def _max_bytes() -> int:
    from core.config import MAX_FILE_SIZE_MB as _MB
    return _MB * 1024 * 1024


# ── 업로드 & 서버사이드 검증 시작 ────────────────────────────────
@router.post("/upload")
async def dashboard_upload(request: Request, bg: BackgroundTasks,
                            file: UploadFile = File(...)):
    fname = (file.filename or "document.pdf").strip()
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    safe_name = sanitize_filename(fname)
    job_id    = generate_job_id()
    job_dir   = get_job_tmp_dir(job_id)
    dest      = job_dir / safe_name

    # 스트리밍으로 바로 파일에 기록 (메모리에 전체 로드 X)
    first_chunk = True
    total_size  = 0
    max_bytes   = _max_bytes()
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
                # 첫 청크에서 PDF 헤더 검사
                if first_chunk:
                    if len(chunk) < 5 or not chunk[:5].startswith(b"%PDF-"):
                        fp.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(400, "유효하지 않은 PDF 파일입니다.")
                    first_chunk = False
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
        "mode":       "server",   # 서버사이드 파이프라인 표시
        "status":     JobStatus.PENDING.value,
        "progress":   0,
        "message":    "검증 대기 중…",
        "filename":   fname,
        "safe_name":  safe_name,
        "file_size":  total_size,
        "created_at": datetime.now().isoformat(),
        "report":     None,
        "error":      None,
    })
    register_ttl(job_id)
    bg.add_task(_run_server_pipeline, job_id, dest, fname)

    logger.info(f"대시보드 업로드: {safe_name} ({total_size/1024:.1f} KB) job={job_id}")
    return JSONResponse({
        "job_id":   job_id,
        "status":   "pending",
        "filename": fname,
        "message":  "서버에서 검증을 시작합니다. 브라우저를 닫아도 계속 처리됩니다.",
    })


# ── 백그라운드 실행 ────────────────────────────────────────────
async def _run_server_pipeline(job_id: str, path: Path, filename: str):
    update_job(job_id, status=JobStatus.PROCESSING.value, message="검증 시작…")
    try:
        pipeline = get_server_pipeline()
        report   = await pipeline.run(job_id, path, filename)
        update_job(job_id,
                   status=JobStatus.COMPLETED.value,
                   progress=100,
                   message="검증 완료",
                   report=report)
    except Exception as e:
        logger.error(f"[{job_id}] 파이프라인 오류: {e}", exc_info=True)
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
