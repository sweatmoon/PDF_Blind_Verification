"""
검증 API 라우터 – 업로드 / 상태 / 리포트 / 다운로드
- 청크 기반 파일 읽기로 대용량 파일 업로드 타임아웃 방지
- 업로드 진행률 제공
"""
from __future__ import annotations
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse, StreamingResponse

from models.schemas import JobStatus
from services.pipeline import Pipeline
from services.file_manager import validate_pdf, register_ttl, delete_job_files
from core.config import (
    get_logger, sanitize_filename, generate_job_id,
    get_job_tmp_dir, get_job, set_job, update_job,
    MAX_FILE_SIZE_MB,
)

router = APIRouter()
logger = get_logger("verify_api")

MAX_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
CHUNK_SIZE = 1024 * 1024  # 1MB 청크


# ── 업로드 & 검증 시작 ─────────────────────────────────────────
@router.post("/upload")
async def upload_and_verify(request: Request,
                             bg: BackgroundTasks,
                             file: UploadFile = File(...)):
    # 파일명 확인
    fname = (file.filename or "document.pdf").strip()
    if not fname.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    safe_name = sanitize_filename(fname)
    job_id    = generate_job_id()
    job_dir   = get_job_tmp_dir(job_id)
    dest      = job_dir / safe_name

    # 파일 청크 기반 읽기 (메모리 효율 + 타임아웃 방지)
    try:
        chunks = []
        total_size = 0
        
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_BYTES:
                raise HTTPException(413, f"파일 크기 초과 (최대 {MAX_FILE_SIZE_MB}MB)")
            chunks.append(chunk)
            await asyncio.sleep(0)  # 이벤트루프 양보
        
        content = b"".join(chunks)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"파일 읽기 오류: {e}")

    if len(content) < 100:
        raise HTTPException(400, "파일이 너무 작거나 비어 있습니다.")
    if not content[:5].startswith(b"%PDF-"):
        raise HTTPException(400, "유효하지 않은 PDF 파일입니다.")

    dest.write_bytes(content)

    # 2차 유효성 검사
    ok, msg = validate_pdf(dest)
    if not ok:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"PDF 검증 실패: {msg}")

    # Job 생성
    set_job(job_id, {
        "job_id":     job_id,
        "status":     JobStatus.PENDING.value,
        "progress":   0,
        "message":    "검증 대기 중…",
        "filename":   fname,          # 원본 파일명 표시
        "safe_name":  safe_name,
        "file_size":  len(content),
        "created_at": datetime.now().isoformat(),
        "report":     None,
        "error":      None,
    })
    register_ttl(job_id)   # 30분 자동 삭제
    bg.add_task(_run, job_id, dest, safe_name)

    logger.info(f"업로드: {safe_name} ({len(content)/1024:.1f} KB) job={job_id}")
    return JSONResponse({
        "job_id":    job_id,
        "status":    "pending",
        "filename":  fname,
        "file_size": len(content),
        "message":   "검증이 시작되었습니다.",
    })


# ── 백그라운드 실행 ────────────────────────────────────────────
async def _run(job_id: str, path: Path, filename: str):
    update_job(job_id, status=JobStatus.PROCESSING.value)
    try:
        pipeline = Pipeline()
        report   = await pipeline.run(job_id, path, filename)
        report_d = report.model_dump(mode="json")
        update_job(job_id,
                   status=JobStatus.COMPLETED.value,
                   progress=100,
                   message="검증 완료",
                   report=report_d)
    except Exception as e:
        logger.error(f"[{job_id}] 파이프라인 오류: {e}", exc_info=True)
        update_job(job_id,
                   status=JobStatus.FAILED.value,
                   progress=0,
                   message=f"검증 실패: {str(e)[:100]}",
                   error=str(e))
        try: delete_job_files(job_id)
        except Exception: pass


# ── 상태 조회 ─────────────────────────────────────────────────
@router.get("/status/{job_id}")
def get_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return JSONResponse({
        "job_id":     job_id,
        "status":     job["status"],
        "progress":   job["progress"],
        "message":    job["message"],
        "filename":   job.get("filename", ""),
        "created_at": job.get("created_at", ""),
        "report":     job["report"] if job["status"] == JobStatus.COMPLETED.value else None,
        "error":      job.get("error"),
    })


# ── 리포트 조회 ───────────────────────────────────────────────
@router.get("/report/{job_id}")
def get_report(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    if job["status"] != JobStatus.COMPLETED.value:
        raise HTTPException(202, f"처리 중: {job['progress']}%")
    report = job.get("report")
    if not report:
        raise HTTPException(404, "리포트 없음")
    return JSONResponse(report)


# ── 리포트 다운로드 ───────────────────────────────────────────
@router.get("/report/{job_id}/download")
def download_report(job_id: str, fmt: str = "json"):
    job = get_job(job_id)
    if not job or job["status"] != JobStatus.COMPLETED.value:
        raise HTTPException(404, "완료된 리포트 없음")
    report = job.get("report", {})

    if fmt == "html":
        html = _build_html(report)
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            content=html,
            headers={"Content-Disposition": f'attachment; filename="report_{job_id[:8]}.html"'})

    # 최소 정보만 포함 (원문 제외)
    minimal = _minimal_report(report)
    return JSONResponse(
        content=minimal,
        headers={"Content-Disposition": f'attachment; filename="report_{job_id[:8]}.json"'},
    )


# ── 내부 헬퍼 ─────────────────────────────────────────────────
def _minimal_report(r: dict) -> dict:
    out = {k: r[k] for k in
           ("job_id","filename","created_at","total_pages",
            "risk_level","violation_count","caution_count","allowed_count",
            "summary","processing_time_seconds") if k in r}
    out["pages"] = []
    for pg in r.get("page_results", []):
        out["pages"].append({
            "page_number":     pg.get("page_number"),
            "violation_count": pg.get("violation_count"),
            "caution_count":   pg.get("caution_count"),
            "detections": [
                {
                    "detection_type":   d.get("detection_type"),
                    "detected_text":    (d.get("detected_text") or "")[:100],
                    "verdict":          d.get("verdict"),
                    "reason":           d.get("reason"),
                    "recommendation":   d.get("recommendation"),
                }
                for d in pg.get("detections", [])
            ],
        })
    return out


def _build_html(r: dict) -> str:
    risk   = r.get("risk_level", "")
    rc     = {"LOW": "#10b981", "MEDIUM": "#f59e0b", "HIGH": "#ef4444"}.get(risk, "#6b7280")
    vc, cc, ac = r.get("violation_count",0), r.get("caution_count",0), r.get("allowed_count",0)

    rows = ""
    for pg in r.get("page_results", []):
        pn = pg.get("page_number", 0)
        label = "메타데이터" if pn == 0 else f"{pn}페이지"
        for d in pg.get("detections", []):
            v  = d.get("verdict","")
            vc2 = {"위반":"#ef4444","주의":"#f59e0b","허용":"#10b981"}.get(v,"#6b7280")
            rows += (f'<tr><td>{label}</td><td>{d.get("detection_type","")}</td>'
                     f'<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                     f'{(d.get("detected_text") or "")[:80]}</td>'
                     f'<td><b style="color:{vc2}">{v}</b></td>'
                     f'<td>{d.get("reason","")}</td>'
                     f'<td>{d.get("recommendation","")}</td></tr>')

    summary = r.get("summary", {})
    notes_html = "".join(f"<li>{n}</li>" for n in summary.get("notes", []))

    return f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="UTF-8"><title>블라인드 검수 리포트</title>
<style>
body{{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:1200px;margin:0 auto;padding:20px;color:#1e293b}}
h1{{border-bottom:2px solid #e2e8f0;padding-bottom:10px;font-size:1.5rem}}
h2{{font-size:1.1rem;margin-top:20px;color:#374151}}
.stats{{display:flex;gap:16px;margin:16px 0;flex-wrap:wrap}}
.card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 20px;text-align:center;min-width:80px}}
.num{{font-size:28px;font-weight:700}}
.badge{{display:inline-block;padding:4px 14px;border-radius:20px;font-weight:700;color:#fff;font-size:14px}}
table{{width:100%;border-collapse:collapse;margin-top:20px;font-size:13px}}
th{{background:#1e293b;color:#fff;padding:9px;text-align:left}}
td{{padding:7px 9px;border-bottom:1px solid #f1f5f9;vertical-align:top}}
tr:nth-child(even){{background:#fafafa}}
.notice{{background:#fef3c7;border:1px solid #fcd34d;padding:10px;border-radius:6px;font-size:12px;margin:8px 0}}
ul{{margin:4px 0;padding-left:20px}}
</style></head><body>
<h1>📋 입찰제안서 블라인드 검수 리포트</h1>
<div class="notice">⚠️ 이 리포트에는 원본 문서가 포함되지 않습니다. 검출 항목 정보만 포함됩니다.</div>
<p><b>파일:</b> {r.get("filename","")} &nbsp;|&nbsp; <b>페이지:</b> {r.get("total_pages",0)}p &nbsp;|&nbsp;
<b>처리시간:</b> {r.get("processing_time_seconds",0)}s &nbsp;|&nbsp;
<b>검증일:</b> {str(r.get("created_at",""))[:19]}</p>
<p><b>블라인드 위험도:</b>
<span class="badge" style="background:{rc}">{risk}</span></p>
<div class="stats">
<div class="card"><div class="num" style="color:#ef4444">{vc}</div><div>위반</div></div>
<div class="card"><div class="num" style="color:#f59e0b">{cc}</div><div>주의</div></div>
<div class="card"><div class="num" style="color:#10b981">{ac}</div><div>허용</div></div>
</div>
<h2>📝 검수 요약</h2>
<ul>{notes_html}</ul>
<h2>🔍 상세 검출 결과</h2>
<table><thead><tr><th>페이지</th><th>검출유형</th><th>검출내용</th>
<th>판정</th><th>판정 사유</th><th>수정 권고</th></tr></thead>
<tbody>{rows or '<tr><td colspan="6" style="text-align:center;padding:20px;color:#6b7280">검출된 위반/주의 항목 없음</td></tr>'}</tbody></table>
<p style="margin-top:30px;font-size:11px;color:#94a3b8">본 리포트는 자동 검수 시스템에 의해 생성되었습니다. | 원본 파일은 검수 즉시 삭제됩니다.</p>
</body></html>"""
