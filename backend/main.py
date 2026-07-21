"""
FastAPI 진입점
"""
import asyncio
import hashlib
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse

from api.verify  import router as verify_router
from api.admin   import router as admin_router
from api.jobs    import router as jobs_router
from api.review  import router as review_router
from services.file_manager import cleanup_scheduler
from core.config import get_logger, CLAUDE_ENABLED, CLAUDE_MODEL, _load_saved_jobs

logger = get_logger("main")

FRONTEND = Path(__file__).parent.parent / "frontend" / "public"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 1. DB 초기화 (스키마 생성 + 기본값 시드) ────────────────
    try:
        from core.database import init_db
        init_db()
        logger.info("SQLite DB 초기화 완료")
    except Exception as e:
        logger.error(f"DB 초기화 실패: {e}")

    # ── 2. DB에서 API 키 복원 (환경변수 없을 때 fallback) ────────
    try:
        import core.config as cfg
        from core.database import kv_get

        # Vision API 키
        if not cfg.GOOGLE_VISION_API_KEY:
            db_vision = kv_get("google_vision_api_key", "")
            if db_vision:
                cfg.GOOGLE_VISION_API_KEY = db_vision
                logger.info("DB에서 Vision API 키 복원")

        # Claude API 키
        if not cfg.ANTHROPIC_API_KEY:
            db_claude = kv_get("claude_api_key", "")
            if db_claude:
                cfg.ANTHROPIC_API_KEY = db_claude
                cfg.CLAUDE_ENABLED = True
                import os
                os.environ["ANTHROPIC_API_KEY"] = db_claude
                logger.info("DB에서 Claude API 키 복원")
    except Exception as e:
        logger.warning(f"DB에서 API 키 복원 실패: {e}")

    # ── 3. 저장된 Job 복원 ─────────────────────────────────────
    _load_saved_jobs()

    # 재시작 전 processing/pending 상태 job → failed 마킹 (중단된 작업 명확히 표시)
    from core.config import list_jobs, update_job
    for job in list_jobs():
        if job.get("status") in ("processing", "pending"):
            update_job(job["job_id"], status="failed", message="서버 재시작으로 작업이 중단됐습니다. 다시 업로드해 주세요.")
            logger.info(f"중단된 job 처리: {job['job_id']}")

    task = asyncio.create_task(cleanup_scheduler())
    logger.info(f"서버 시작 | Claude={'활성 ' + CLAUDE_MODEL if CLAUDE_ENABLED else '비활성(규칙 기반)'}")
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    logger.info("서버 종료")


app = FastAPI(
    title="입찰 제안서 블라인드 검증 서비스",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(verify_router,  prefix="/api/verify",  tags=["검증"])
app.include_router(admin_router,   prefix="/api/admin",   tags=["관리자"])
app.include_router(jobs_router,    prefix="/api/jobs",    tags=["대시보드"])
app.include_router(review_router,  prefix="/api/review",  tags=["제안서검수"])


# ── 정적 파일 & SPA 폴백 ──────────────────────────────────────
MIME = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css",   ".json": "application/json",
    ".png": "image/png",  ".jpg": "image/jpeg",
    ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

@app.get("/", include_in_schema=False)
async def root():
    """루트 경로 — index.html을 UTF-8로 서빙"""
    index = FRONTEND / "index.html"
    if index.exists():
        content = index.read_bytes()
        import hashlib as _hl
        etag = _hl.md5(content).hexdigest()
        return Response(
            content=content,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma":        "no-cache",
                "Expires":       "0",
                "ETag":          f'"{etag}"',
                "Vary":          "*",
                "X-Content-Ver": etag[:8],
                "Clear-Site-Data": '"cache"',
            },
        )
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str, request: Request):
    # API 경로는 제외
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    # 정적 파일 우선
    candidate = FRONTEND / full_path
    if candidate.is_file():
        mt = MIME.get(candidate.suffix.lower(), "application/octet-stream")
        content = candidate.read_bytes()
        # 모든 HTML은 캐시 완전 금지 + ETag으로 프록시 우회
        if candidate.suffix.lower() == ".html":
            etag = hashlib.md5(content).hexdigest()
            return Response(
                content=content, media_type=mt,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                    "Pragma":        "no-cache",
                    "Expires":       "0",
                    "ETag":          f'"{etag}"',
                    "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(candidate.stat().st_mtime)),
                    "Vary":          "*",
                    "X-Content-Ver": etag[:8],
                    "Clear-Site-Data": '"cache"',
                })
        return Response(content=content, media_type=mt)

    # SPA index.html 폴백
    index = FRONTEND / "index.html"
    if index.exists():
        content = index.read_bytes()
        etag = hashlib.md5(content).hexdigest()
        return Response(
            content=content,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma":        "no-cache",
                "Expires":       "0",
                "ETag":          f'"{etag}"',
                "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(index.stat().st_mtime)),
                "Vary":          "*",
                "X-Content-Ver": etag[:8],
                "Clear-Site-Data": '"cache"',
            },
        )
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)


@app.exception_handler(Exception)
async def _err(req: Request, exc: Exception):
    logger.error(f"미처리 예외: {exc}", exc_info=True)
    return JSONResponse({"detail": "서버 내부 오류"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
