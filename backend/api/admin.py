"""
관리자 API – 사전 관리 / 시스템 설정 / 통계
- 사전: SQLite DB 영구 저장 (Railway 재배포 후에도 유지)
- API 키: DB kv_store + 파일 이중 저장
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from models.schemas import DictionaryUpdateRequest
from services.rule_detector import get_rule_detector
from core.config import get_logger, load_dict, save_dict
from core.database import (
    db_load_dict, db_save_dict, db_add_terms, db_remove_term,
    db_replace_subkey, db_count_subkey, kv_set, kv_get,
)

router = APIRouter()
logger = get_logger("admin_api")


# ── 사전 전체 조회 ────────────────────────────────────────────
@router.get("/dictionary")
def get_dictionary():
    data = load_dict()
    return JSONResponse(data)


# ── 사전 전체 교체 (localStorage 백업 복원용) ──────────────────
@router.post("/dictionary/restore")
def restore_dictionary(body: dict):
    """
    localStorage 백업에서 사전 전체를 한 번에 복원.
    body: { direct_identifiers: {...}, indirect_identifiers: {...}, allowed_terms: {...} }
    """
    valid_groups = {"direct_identifiers", "indirect_identifiers", "allowed_terms"}
    restore_data: dict = {}

    for group_key in valid_groups:
        if group_key not in body:
            continue
        group_data = body[group_key]
        if not isinstance(group_data, dict):
            continue
        restore_data[group_key] = {}
        for subkey, items in group_data.items():
            if isinstance(items, list):
                restore_data[group_key][subkey] = [
                    i.strip() for i in items if str(i).strip()
                ]

    if not db_save_dict(restore_data):
        raise HTTPException(500, "사전 DB 저장 실패")

    get_rule_detector().reload()
    total = sum(
        len(v) for g in valid_groups
        for v in restore_data.get(g, {}).values()
    )
    logger.info(f"사전 복원 완료 (DB): 총 {total}개 항목")
    return JSONResponse({"success": True, "total_items": total})


# ── 사전 업데이트 ─────────────────────────────────────────────
@router.put("/dictionary")
def update_dictionary(req: DictionaryUpdateRequest):
    valid_groups = {"direct_identifiers", "indirect_identifiers", "allowed_terms"}
    if req.group not in valid_groups:
        raise HTTPException(400, f"유효하지 않은 그룹: {req.group}")

    clean = [i.strip() for i in req.items if i.strip()]

    if req.action == "add":
        added, skipped = db_add_terms(req.group, req.subcategory, clean)
        total = db_count_subkey(req.group, req.subcategory)
    elif req.action == "remove":
        for item in clean:
            db_remove_term(req.group, req.subcategory, item)
        total = db_count_subkey(req.group, req.subcategory)
        added = skipped = 0
    elif req.action == "replace":
        db_replace_subkey(req.group, req.subcategory, clean)
        total = db_count_subkey(req.group, req.subcategory)
        added = skipped = 0
    else:
        raise HTTPException(400, f"유효하지 않은 action: {req.action}")

    get_rule_detector().reload()
    resp: dict = {
        "success": True,
        "group":   req.group,
        "subcat":  req.subcategory,
        "count":   total,
    }
    if req.action == "add":
        resp["added"]   = added
        resp["skipped"] = skipped
    return JSONResponse(resp)


# ── 단일 항목 추가 ────────────────────────────────────────────
@router.post("/dictionary/{group}/{subcategory}")
def add_item(group: str, subcategory: str, body: dict):
    # 'term' 또는 'item' 필드 지원
    item = (body.get("term") or body.get("item") or "").strip()
    if not item:
        raise HTTPException(400, "term 또는 item 값 필요")
    return update_dictionary(DictionaryUpdateRequest(
        group=group, subcategory=subcategory, items=[item], action="add"))


# ── 배치 항목 추가 (다중 입력, DB 직접 UPSERT) ───────────────
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

    added, skipped = db_add_terms(group, subcategory, clean)
    total = db_count_subkey(group, subcategory)

    get_rule_detector().reload()
    return JSONResponse({"success": True, "added": added, "skipped": skipped, "total": total})


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
        "auto_delete_minutes":      cfg.AUTO_DELETE_MIN,
        "max_file_size_mb":         cfg.MAX_FILE_SIZE_MB,
        "ocr_enabled":              cfg.OCR_ENABLED,
        "claude_enabled":           cfg.CLAUDE_ENABLED,
        "claude_model":             cfg.CLAUDE_MODEL,
        "google_vision_configured": bool(cfg.get_google_vision_key()),
        "allowed_formats":          ["PDF"],
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

    # 파일에 영구 저장 (하위 호환) + DB 저장
    try:
        cfg._CLAUDE_KEY_FILE.write_text(api_key, encoding="utf-8")
    except Exception:
        pass
    try:
        from core.database import kv_set
        kv_set("claude_api_key", api_key)
    except Exception:
        pass

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


# ── Google Vision API 키 런타임 설정 ──────────────────────────
class VisionKeyRequest(BaseModel):
    api_key: str

@router.post("/vision-key")
def set_vision_key(req: VisionKeyRequest):
    import core.config as cfg

    api_key = req.api_key.strip()
    if not api_key:
        raise HTTPException(400, "API 키 필요")

    cfg.set_google_vision_key(api_key)   # DB + 파일 이중 저장
    logger.info("Google Vision API 키 런타임 업데이트 완료")
    return JSONResponse({"success": True, "vision_enabled": True})

@router.get("/vision-key")
def get_vision_key_status():
    import core.config as cfg
    key = cfg.get_google_vision_key()
    masked = (key[:8] + "…" + key[-4:]) if len(key) > 12 else ("설정됨" if key else "")
    return JSONResponse({"configured": bool(key), "masked": masked})


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
