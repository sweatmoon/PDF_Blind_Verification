"""
중앙 설정 및 공통 유틸리티
"""
import os, uuid, json, logging, re
from pathlib import Path

# ── 경로 ──────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent.parent          # backend/
ROOT_DIR  = BASE_DIR.parent                       # webapp/
TMP_DIR   = ROOT_DIR / "tmp"
LOGS_DIR  = ROOT_DIR / "logs"
DATA_DIR  = ROOT_DIR / "data"

for _d in (TMP_DIR, LOGS_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 환경 변수 ──────────────────────────────────────────────────
# 키 파일 경로 (환경변수보다 먼저 정의)
_VISION_KEY_FILE = DATA_DIR / "vision_api_key.txt"
_CLAUDE_KEY_FILE = DATA_DIR / "claude_api_key.txt"

def _load_key_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""
    except Exception:
        return ""

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "") or _load_key_file(_CLAUDE_KEY_FILE)
MAX_FILE_SIZE_MB   = int(os.getenv("MAX_FILE_SIZE_MB", "300"))
AUTO_DELETE_MIN    = int(os.getenv("AUTO_DELETE_MIN", "30"))
OCR_ENABLED        = os.getenv("OCR_ENABLED", "true").lower() == "true"
CLAUDE_ENABLED     = bool(ANTHROPIC_API_KEY)
CLAUDE_MODEL       = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── Google Vision API (런타임 설정 가능 + DB 영구 저장) ─────────
GOOGLE_VISION_API_KEY: str = os.getenv("GOOGLE_VISION_API_KEY", "") or _load_key_file(_VISION_KEY_FILE)

def set_google_vision_key(key: str):
    global GOOGLE_VISION_API_KEY
    GOOGLE_VISION_API_KEY = key.strip()
    # DB + 파일 이중 저장 (하위 호환)
    try:
        from core.database import kv_set
        kv_set("google_vision_api_key", key.strip())
    except Exception:
        pass
    try:
        _VISION_KEY_FILE.write_text(key.strip(), encoding="utf-8")
    except Exception:
        pass

def get_google_vision_key() -> str:
    return GOOGLE_VISION_API_KEY

def _load_vision_key_from_db() -> str:
    """DB에서 Vision API 키 로드 (init_db 이후 호출)"""
    try:
        from core.database import kv_get
        return kv_get("google_vision_api_key", "")
    except Exception:
        return ""

# ── 로거 팩토리 ────────────────────────────────────────────────
def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        sh = logging.StreamHandler(); sh.setFormatter(fmt)
        fh = logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8")
        fh.setFormatter(fmt)
        lg.addHandler(sh); lg.addHandler(fh)
    return lg

logger = get_logger("config")

# ── 파일명 sanitize ────────────────────────────────────────────
def sanitize_filename(filename: str) -> str:
    filename = os.path.basename(filename)
    filename = re.sub(r"[^\w\s\-.]", "", filename)
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:196] + ext
    return filename or "document.pdf"

def generate_job_id() -> str:
    return str(uuid.uuid4())

def get_job_tmp_dir(job_id: str) -> Path:
    d = TMP_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d

# ── Job 스토어 (인메모리 + 파일 영구 저장) ───────────────────────
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

_jobs: dict[str, dict] = {}

def _job_file(job_id: str) -> Path:
    return REPORTS_DIR / f"{job_id}.json"

def _save_job_file(job_id: str, data: dict):
    """완료/실패 job을 파일로 영구 저장 (report 포함)"""
    try:
        _job_file(job_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), "utf-8")
    except Exception as e:
        logger.warning(f"job 파일 저장 실패 {job_id}: {e}")

def _load_saved_jobs():
    """서버 시작 시 저장된 job 목록 복원 (report 제외, 메타만)"""
    loaded = 0
    for f in sorted(REPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
        try:
            data = json.loads(f.read_text("utf-8"))
            jid = data.get("job_id")
            if jid and jid not in _jobs:
                # 메모리엔 report 제외한 요약만 저장 (메모리 절약)
                summary = {k: v for k, v in data.items() if k != "report"}
                summary["has_report"] = data.get("report") is not None
                _jobs[jid] = summary
                loaded += 1
        except Exception:
            pass
    if loaded:
        logger.info(f"저장된 job {loaded}개 복원")

def set_job(job_id: str, data: dict):
    _jobs[job_id] = data
    # 모든 상태 즉시 파일 저장 (서버 재시작 복원용)
    _save_job_file(job_id, data)

def get_job(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if job is None:
        # 메모리에 없으면 파일에서 복원 시도
        try:
            data = json.loads(_job_file(job_id).read_text("utf-8"))
            _jobs[job_id] = {k: v for k, v in data.items() if k != "report"}
            _jobs[job_id]["has_report"] = data.get("report") is not None
            job = _jobs[job_id]
        except Exception:
            return None
    if job and job.get("has_report") and job.get("report") is None:
        # 파일에서 report 로드
        try:
            data = json.loads(_job_file(job_id).read_text("utf-8"))
            job["report"] = data.get("report")
            job["has_report"] = True
        except Exception:
            pass
    return job

def update_job(job_id: str, **kw):
    if job_id in _jobs:
        _jobs[job_id].update(kw)
        status = _jobs[job_id].get("status", "")
        # 모든 상태 파일 저장 (processing 포함 — 서버 재시작 복원용)
        # report는 completed일 때만 저장 (용량 절약)
        save_data = {k: v for k, v in _jobs[job_id].items() if k != "report"}
        if status == "completed":
            save_data = _jobs[job_id]  # report 포함
        _save_job_file(job_id, save_data)

def delete_job(job_id: str):
    _jobs.pop(job_id, None)
    _job_file(job_id).unlink(missing_ok=True)

def list_jobs() -> list:
    return list(_jobs.values())

# ── 사전 기본값 (DB 시드용) ──────────────────────────────────
DEFAULT_DICT: dict = {
    "direct_identifiers": {
        "company_names":        [],
        "english_names":        [],
        "abbreviations":        [],
        "representative_names": [],
        "personnel_names":      [],
        "emails":               [],
        "urls":                 [],
        "domains":              [],
        "brand_names":          [],
    },
    "indirect_identifiers": {
        "color_names":    ["ActivoRED", "Samsung Blue", "SK Red"],
        "solution_names": [],
        "slogans":        [],
        "org_names":      [],
        "service_names":  [],
    },
    "allowed_terms": {
        "client_names":         [],
        "client_abbreviations": [],
        "project_names":        [],
        "official_institutions": [
            "행정안전부","과학기술정보통신부","기획재정부","국방부","교육부",
            "국토교통부","보건복지부","환경부","고용노동부",
            "한국도로공사","한국전력공사","한국수자원공사","한국토지주택공사",
            "건강보험심사평가원","국민건강보험공단","국민연금공단",
        ],
    },
}

# ── 사전 로드/저장 → SQLite DB 위임 ──────────────────────────
def load_dict() -> dict:
    """DB에서 사전 로드. DB 미준비 시 DEFAULT_DICT 반환."""
    try:
        from core.database import db_load_dict
        return db_load_dict()
    except Exception as e:
        logger.warning(f"DB 사전 로드 실패, 기본값 사용: {e}")
        return {g: {sk: list(v) for sk, v in subs.items()} for g, subs in DEFAULT_DICT.items()}

def save_dict(data: dict) -> bool:
    """DB에 사전 저장."""
    try:
        from core.database import db_save_dict
        return db_save_dict(data)
    except Exception as e:
        logger.error(f"DB 사전 저장 실패: {e}")
        return False
