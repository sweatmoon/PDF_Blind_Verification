"""
제안서 검수 API 라우터
- 4개 파일 multipart 업로드 → Claude Sonnet 분석 → job_id 반환
- 상태 조회, 리포트 조회
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Form
from fastapi.responses import JSONResponse

from core.config import (
    get_logger, generate_job_id, now_kst_iso,
    get_job, set_job, update_job, list_jobs,
    ANTHROPIC_API_KEY, DATA_DIR,
)

router = APIRouter()
logger = get_logger("review_api")

MAX_FILE_MB = 50
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

ALLOWED_EXTS = {
    "audit_rfp":    {".pdf", ".hwp", ".hwpx", ".docx", ".txt"},
    "target_rfp":   {".pdf", ".hwp", ".hwpx", ".docx", ".txt"},
    "portal_html":  {".html", ".htm", ".txt"},
    "proposal_ppt": {".pptx", ".ppt", ".pdf"},
}


def _check_ext(field: str, filename: str):
    ext = Path(filename).suffix.lower()
    allowed = ALLOWED_EXTS.get(field, set())
    if allowed and ext not in allowed:
        raise HTTPException(
            400,
            f"'{field}' 파일은 {', '.join(sorted(allowed))} 형식만 허용됩니다. (받은 파일: {filename})"
        )


# ── 업로드 & 검수 시작 ──────────────────────────────────────────
@router.post("/upload")
async def upload_and_review(
    bg: BackgroundTasks,
    audit_rfp:    UploadFile = File(..., description="감리사업 RFP (PDF/HWP/HWPX/DOCX)"),
    target_rfp:   UploadFile = File(..., description="대상사업 RFP (PDF/HWP/HWPX/DOCX)"),
    portal_html:  UploadFile = File(..., description="포털 제안작업표 HTML"),
    proposal_ppt: UploadFile = File(..., description="제안서 PPT (PPTX/PDF)"),
):
    """4개 파일을 받아 Claude Sonnet으로 제안서 검수를 시작합니다."""

    # API 키 체크
    key = ANTHROPIC_API_KEY
    if not key:
        # DB fallback
        try:
            from core.database import kv_get
            key = kv_get("claude_api_key", "") or ""
        except Exception:
            pass
    if not key:
        raise HTTPException(503, "Claude API 키가 설정되지 않았습니다. 관리자 페이지에서 설정해주세요.")

    # 파일명 확인
    files = {
        "audit_rfp":    audit_rfp,
        "target_rfp":   target_rfp,
        "portal_html":  portal_html,
        "proposal_ppt": proposal_ppt,
    }
    for field, f in files.items():
        fname = f.filename or ""
        if not fname:
            raise HTTPException(400, f"'{field}' 파일이 비어 있습니다.")
        _check_ext(field, fname)

    # 파일 읽기
    file_data: dict[str, tuple[bytes, str]] = {}
    for field, f in files.items():
        try:
            data = await f.read()
        except Exception as e:
            raise HTTPException(500, f"'{field}' 파일 읽기 오류: {e}")
        if len(data) < 10:
            raise HTTPException(400, f"'{field}' 파일이 너무 작습니다.")
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"'{field}' 파일이 너무 큽니다. (최대 {MAX_FILE_MB}MB)")
        file_data[field] = (data, f.filename or "")

    # Job 생성
    job_id = generate_job_id()
    filenames = {k: v[1] for k, v in file_data.items()}
    set_job(job_id, {
        "job_id":     job_id,
        "type":       "review",          # 블라인드 검증과 구분
        "status":     "pending",
        "progress":   0,
        "message":    "검수 대기 중…",
        "filenames":  filenames,
        "created_at": now_kst_iso(),
        "report":     None,
        "error":      None,
    })

    # 백그라운드 실행
    bg.add_task(_run_review, job_id, file_data, key)

    logger.info(
        f"[review] 업로드 완료 job={job_id} | "
        + " ".join(f"{k}={v[1]}" for k, v in file_data.items())
    )
    return JSONResponse({
        "job_id":    job_id,
        "status":    "pending",
        "filenames": filenames,
        "message":   "제안서 검수가 시작되었습니다. 1~3분 소요될 수 있습니다.",
    })


# ── 백그라운드 검수 실행 ─────────────────────────────────────────
async def _run_review(job_id: str, file_data: dict, api_key: str):
    update_job(job_id, status="processing", progress=5, message="문서 텍스트 추출 중…")
    try:
        from services.review_service import run_review

        loop = asyncio.get_event_loop()

        def _blocking():
            return run_review(
                audit_rfp_data=file_data["audit_rfp"][0],
                audit_rfp_name=file_data["audit_rfp"][1],
                target_rfp_data=file_data["target_rfp"][0],
                target_rfp_name=file_data["target_rfp"][1],
                portal_html_data=file_data["portal_html"][0],
                portal_html_name=file_data["portal_html"][1],
                proposal_ppt_data=file_data["proposal_ppt"][0],
                proposal_ppt_name=file_data["proposal_ppt"][1],
                api_key=api_key,
            )

        update_job(job_id, progress=20, message="Claude AI 분석 중… (1~3분 소요)")

        with ThreadPoolExecutor(max_workers=1) as ex:
            result = await loop.run_in_executor(ex, _blocking)

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="검수 완료",
            report=result,
        )
        logger.info(f"[review] 완료 job={job_id}")

    except Exception as e:
        logger.error(f"[review] 오류 job={job_id}: {e}", exc_info=True)
        update_job(
            job_id,
            status="failed",
            progress=0,
            message=f"검수 실패: {str(e)[:150]}",
            error=str(e),
        )


# ── 상태 조회 ────────────────────────────────────────────────────
@router.get("/status/{job_id}")
def get_review_status(job_id: str):
    job = get_job(job_id)
    if not job or job.get("type") != "review":
        raise HTTPException(404, "검수 작업을 찾을 수 없습니다.")
    return JSONResponse({
        "job_id":     job_id,
        "status":     job["status"],
        "progress":   job["progress"],
        "message":    job["message"],
        "filenames":  job.get("filenames", {}),
        "created_at": job.get("created_at", ""),
        "report":     job["report"] if job["status"] == "completed" else None,
        "error":      job.get("error"),
    })


# ── 리포트 조회 ──────────────────────────────────────────────────
@router.get("/report/{job_id}")
def get_review_report(job_id: str):
    job = get_job(job_id)
    if not job or job.get("type") != "review":
        raise HTTPException(404, "검수 작업을 찾을 수 없습니다.")
    if job["status"] != "completed":
        raise HTTPException(202, f"처리 중: {job['progress']}%")
    report = job.get("report")
    if not report:
        raise HTTPException(404, "리포트 없음")
    return JSONResponse(report)


# ── 디버그: 파싱 실패 원인 조회 ──────────────────────────────────
@router.get("/debug/{job_id}")
def get_review_debug(job_id: str):
    """파싱 실패 시 Claude 원본 응답을 확인하는 개발용 엔드포인트."""
    job = get_job(job_id)
    if not job or job.get("type") != "review":
        raise HTTPException(404, "검수 작업을 찾을 수 없습니다.")
    report = job.get("report") or {}
    return JSONResponse({
        "job_id":       job_id,
        "status":       job.get("status"),
        "stop_reason":  report.get("_stop_reason", "N/A"),
        "parse_failed": report.get("id") == "parse-error",
        "debug_raw_head": report.get("_debug_raw", "")[:2000],
        "debug_raw_tail": report.get("_debug_tail", ""),
        "error":        job.get("error"),
    })


# ── 검수 모델 조회/변경 ──────────────────────────────────────────
@router.get("/model")
def get_model():
    from services.review_service import get_review_model
    return JSONResponse({"model": get_review_model()})


@router.post("/model")
def set_model(body: dict):
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "model 값이 비어 있습니다.")
    from services.review_service import set_review_model
    set_review_model(model)
    logger.info(f"[review] 모델 변경: {model}")
    return JSONResponse({"success": True, "model": model})
