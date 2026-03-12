"""
관리자 API – 사전 관리 / 시스템 설정 / 통계
- Claude API 키 런타임 업데이트 추가
- 단일 항목 추가/삭제 'term' 필드 지원
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
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
    # 'term' 또는 'item' 필드 지원
    item = (body.get("term") or body.get("item") or "").strip()
    if not item:
        raise HTTPException(400, "term 또는 item 값 필요")
    return update_dictionary(DictionaryUpdateRequest(
        group=group, subcategory=subcategory, items=[item], action="add"))


# ── 배치 항목 추가 (다중 입력, race condition 없음) ────────────
@router.post("/dictionary/{group}/{subcategory}/batch")
def add_items_batch(group: str, subcategory: str, body: dict):
    terms = body.get("terms", [])
    if not terms or not isinstance(terms, list):
        raise HTTPException(400, "terms 배열 필요")
    clean = [str(t).strip() for t in terms if str(t).strip()]
    if not clean:
        raise HTTPException(400, "유효한 항목 없음")

    valid_groups = {"direct_identifiers", "indirect_identifiers", "allowed_terms"}
    if group not in valid_groups:
        raise HTTPException(400, f"유효하지 않은 그룹: {group}")

    data  = load_dict()
    grp   = data.setdefault(group, {})
    grp.setdefault(subcategory, [])
    existing = set(grp[subcategory])

    added = 0
    for item in clean:
        if item not in existing:
            grp[subcategory].append(item)
            existing.add(item)
            added += 1

    skipped = len(clean) - added
    if not save_dict(data):
        raise HTTPException(500, "사전 저장 실패")
    get_rule_detector().reload()
    return JSONResponse({"success": True, "added": added, "skipped": skipped,
                         "total": len(grp[subcategory])})


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
    import core.config as cfg
    return JSONResponse({
        "auto_delete_minutes": cfg.AUTO_DELETE_MIN,
        "max_file_size_mb":    cfg.MAX_FILE_SIZE_MB,
        "ocr_enabled":         cfg.OCR_ENABLED,
        "claude_enabled":      cfg.CLAUDE_ENABLED,
        "claude_model":        cfg.CLAUDE_MODEL,
        "allowed_formats":     ["PDF"],
    })


# ── Claude API 키 런타임 설정 ─────────────────────────────────
class ClaudeKeyRequest(BaseModel):
    api_key: str

@router.post("/claude-key")
def set_claude_key(req: ClaudeKeyRequest):
    import core.config as cfg
    from services.claude_judge import _reset_judge
    
    api_key = req.api_key.strip()
    if not api_key:
        raise HTTPException(400, "API 키 필요")
    if not api_key.startswith("sk-ant-"):
        raise HTTPException(400, "올바르지 않은 Anthropic API 키 형식")

    # 런타임 설정 업데이트
    cfg.ANTHROPIC_API_KEY = api_key
    cfg.CLAUDE_ENABLED = True

    # Claude 클라이언트 재초기화
    try:
        import os
        os.environ["ANTHROPIC_API_KEY"] = api_key
        _reset_judge()
        logger.info("Claude API 키 런타임 업데이트 완료")
        return JSONResponse({"success": True, "claude_enabled": True, "model": cfg.CLAUDE_MODEL})
    except Exception as e:
        logger.error(f"Claude 재초기화 오류: {e}")
        raise HTTPException(500, f"Claude 초기화 실패: {e}")


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
