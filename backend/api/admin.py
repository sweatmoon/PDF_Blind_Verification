"""
관리자 API – 사전 관리 / 시스템 설정 / 통계
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from models.schemas import DictionaryUpdateRequest
from services.rule_detector import get_rule_detector
from core.config import get_logger, load_dict, save_dict

router = APIRouter()
logger = get_logger("admin_api")


# ── 사전 전체 조회 ────────────────────────────────────────────
@router.get("/dictionary")
def get_dictionary():
    data = load_dict()
    return JSONResponse(data)


# ── 사전 업데이트 ─────────────────────────────────────────────
@router.put("/dictionary")
def update_dictionary(req: DictionaryUpdateRequest):
    valid_groups = {"direct_identifiers", "indirect_identifiers", "allowed_terms"}
    if req.group not in valid_groups:
        raise HTTPException(400, f"유효하지 않은 그룹: {req.group}")

    data  = load_dict()
    group = data.setdefault(req.group, {})
    group.setdefault(req.subcategory, [])

    clean = [i.strip() for i in req.items if i.strip()]

    if req.action == "add":
        existing = set(group[req.subcategory])
        for item in clean:
            if item not in existing:
                group[req.subcategory].append(item)
                existing.add(item)
    elif req.action == "remove":
        rm = set(clean)
        group[req.subcategory] = [x for x in group[req.subcategory] if x not in rm]
    elif req.action == "replace":
        group[req.subcategory] = clean
    else:
        raise HTTPException(400, f"유효하지 않은 action: {req.action}")

    if not save_dict(data):
        raise HTTPException(500, "사전 저장 실패")

    get_rule_detector().reload()
    return JSONResponse({
        "success": True,
        "group":   req.group,
        "subcat":  req.subcategory,
        "count":   len(group[req.subcategory]),
    })


# ── 단일 항목 추가 ────────────────────────────────────────────
@router.post("/dictionary/{group}/{subcategory}")
def add_item(group: str, subcategory: str, body: dict):
    item = (body.get("item") or "").strip()
    if not item:
        raise HTTPException(400, "item 값 필요")
    return update_dictionary(DictionaryUpdateRequest(
        group=group, subcategory=subcategory, items=[item], action="add"))


# ── 단일 항목 삭제 ────────────────────────────────────────────
@router.delete("/dictionary/{group}/{subcategory}/{item:path}")
def delete_item(group: str, subcategory: str, item: str):
    from urllib.parse import unquote
    item = unquote(item).strip()
    return update_dictionary(DictionaryUpdateRequest(
        group=group, subcategory=subcategory, items=[item], action="remove"))


# ── 시스템 설정 조회 ──────────────────────────────────────────
@router.get("/config")
def get_config():
    from core.config import AUTO_DELETE_MIN, MAX_FILE_SIZE_MB, OCR_ENABLED, CLAUDE_ENABLED, CLAUDE_MODEL
    return JSONResponse({
        "auto_delete_minutes": AUTO_DELETE_MIN,
        "max_file_size_mb":    MAX_FILE_SIZE_MB,
        "ocr_enabled":         OCR_ENABLED,
        "claude_enabled":      CLAUDE_ENABLED,
        "claude_model":        CLAUDE_MODEL,
        "allowed_formats":     ["PDF"],
    })


# ── 저장소 통계 ───────────────────────────────────────────────
@router.get("/stats")
def get_stats():
    from services.file_manager import storage_stats
    from core.config import list_jobs
    jobs = list_jobs()
    st   = {s: sum(1 for j in jobs if j.get("status") == s)
            for s in ("pending","processing","completed","failed")}
    return JSONResponse({"storage": storage_stats(), "jobs": st})


# ── 로그 조회 (원문 마스킹) ───────────────────────────────────
@router.get("/logs")
def get_logs(lines: int = 80):
    from core.config import LOGS_DIR
    f = LOGS_DIR / "app.log"
    if not f.exists():
        return JSONResponse({"logs": []})
    try:
        all_lines = f.read_text("utf-8").splitlines()
        recent = all_lines[-lines:]
        return JSONResponse({"logs": recent})
    except Exception as e:
        return JSONResponse({"logs": [f"로그 읽기 오류: {e}"]})
