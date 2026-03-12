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
MAX_FILE_SIZE_MB   = int(os.getenv("MAX_FILE_SIZE_MB", "100"))
AUTO_DELETE_MIN    = int(os.getenv("AUTO_DELETE_MIN", "30"))
OCR_ENABLED        = os.getenv("OCR_ENABLED", "true").lower() == "true"
CLAUDE_ENABLED     = bool(ANTHROPIC_API_KEY)
CLAUDE_MODEL       = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ── Google Vision API (런타임 설정 가능 + 파일 영구 저장) ────────
GOOGLE_VISION_API_KEY: str = os.getenv("GOOGLE_VISION_API_KEY", "") or _load_key_file(_VISION_KEY_FILE)

def set_google_vision_key(key: str):
    global GOOGLE_VISION_API_KEY
    GOOGLE_VISION_API_KEY = key.strip()
    try:
        _VISION_KEY_FILE.write_text(key.strip(), encoding="utf-8")
    except Exception:
        pass

def get_google_vision_key() -> str:
    return GOOGLE_VISION_API_KEY

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

# ── 인메모리 Job 스토어 ────────────────────────────────────────
_jobs: dict[str, dict] = {}

def set_job(job_id: str, data: dict):           _jobs[job_id] = data
def get_job(job_id: str) -> dict | None:        return _jobs.get(job_id)
def update_job(job_id: str, **kw):
    if job_id in _jobs: _jobs[job_id].update(kw)
def list_jobs() -> list:                        return list(_jobs.values())

# ── 사전 파일 ─────────────────────────────────────────────────
DICT_FILE = DATA_DIR / "dictionary.json"

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

def load_dict() -> dict:
    if DICT_FILE.exists():
        try:
            data = json.loads(DICT_FILE.read_text("utf-8"))
            # 누락 키 병합
            for g in DEFAULT_DICT:
                data.setdefault(g, {})
                for sk in DEFAULT_DICT[g]:
                    data[g].setdefault(sk, DEFAULT_DICT[g][sk])
            return data
        except Exception as e:
            logger.warning(f"사전 로드 실패: {e}")
    return {g: {sk: list(v) for sk, v in subs.items()} for g, subs in DEFAULT_DICT.items()}

def save_dict(data: dict) -> bool:
    try:
        DICT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        return True
    except Exception as e:
        logger.error(f"사전 저장 실패: {e}"); return False
