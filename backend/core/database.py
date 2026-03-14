"""
SQLite 영구 저장소
- 사전(dictionary) + API 키를 파일이 아닌 DB로 관리
- DB 파일: DATA_DIR/blind_verify.db  (Railway Volume 마운트 경로)
- 스레드 안전: check_same_thread=False + 커넥션 풀 없이 단일 파일 직접 사용
  (FastAPI는 단일 프로세스이므로 충분)

테이블 구조
  dictionary_items  : group_key, subkey, term  (복합 UK)
  kv_store          : key, value               (API 키, 기타 설정)
"""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from typing import Any

from core.config import DATA_DIR, get_logger, DEFAULT_DICT

logger = get_logger("database")

DB_PATH = DATA_DIR / "blind_verify.db"

# ── 스레드 로컬 커넥션 (각 스레드마다 독립 커넥션) ─────────────
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # 동시 읽기 성능 향상
        conn.execute("PRAGMA synchronous=NORMAL") # 속도 vs 안전 균형
        _local.conn = conn
    return _local.conn


# ── 스키마 초기화 ─────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictionary_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key  TEXT NOT NULL,
    subkey     TEXT NOT NULL,
    term       TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_key, subkey, term)
);

CREATE INDEX IF NOT EXISTS idx_dict_group  ON dictionary_items(group_key);
CREATE INDEX IF NOT EXISTS idx_dict_subkey ON dictionary_items(group_key, subkey);

CREATE TABLE IF NOT EXISTS kv_store (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db() -> None:
    """앱 시작 시 1회 호출. 테이블 생성 + 기본값 시드."""
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    conn.commit()

    # 빈 DB면 DEFAULT_DICT 시드
    cur = conn.execute("SELECT COUNT(*) FROM dictionary_items")
    count = cur.fetchone()[0]
    if count == 0:
        logger.info("사전 DB 최초 초기화 — 기본값 삽입")
        _seed_default(conn)
    else:
        logger.info(f"사전 DB 로드 완료 ({count}개 항목)")


def _seed_default(conn: sqlite3.Connection) -> None:
    rows = []
    for group_key, subcats in DEFAULT_DICT.items():
        for subkey, terms in subcats.items():
            for term in terms:
                if term.strip():
                    rows.append((group_key, subkey, term.strip()))
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO dictionary_items (group_key, subkey, term) VALUES (?,?,?)",
            rows,
        )
        conn.commit()


# ── 사전 CRUD ─────────────────────────────────────────────────

def db_load_dict() -> dict:
    """DB에서 사전 전체 로드 → config.DEFAULT_DICT 구조와 동일한 dict 반환"""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT group_key, subkey, term FROM dictionary_items ORDER BY group_key, subkey, id"
    )
    result: dict[str, dict[str, list]] = {}
    for row in cur.fetchall():
        g, s, t = row["group_key"], row["subkey"], row["term"]
        result.setdefault(g, {}).setdefault(s, []).append(t)

    # 누락 키 보완 (DEFAULT_DICT 구조 유지)
    for group, subcats in DEFAULT_DICT.items():
        result.setdefault(group, {})
        for subkey in subcats:
            result[group].setdefault(subkey, [])
    return result


def db_save_dict(data: dict) -> bool:
    """사전 전체 저장 (기존 항목 삭제 후 재삽입 방식 — 완전 교체)"""
    conn = _get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM dictionary_items")
        rows = []
        for group_key, subcats in data.items():
            if not isinstance(subcats, dict):
                continue
            for subkey, terms in subcats.items():
                if not isinstance(terms, list):
                    continue
                for term in terms:
                    t = str(term).strip()
                    if t:
                        rows.append((group_key, subkey, t))
        conn.executemany(
            "INSERT OR IGNORE INTO dictionary_items (group_key, subkey, term) VALUES (?,?,?)",
            rows,
        )
        conn.execute("COMMIT")
        return True
    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"사전 DB 저장 실패: {e}")
        return False


def db_add_terms(group_key: str, subkey: str, terms: list[str]) -> tuple[int, int]:
    """항목 추가. (added, skipped) 반환"""
    conn = _get_conn()
    added = skipped = 0
    for term in terms:
        t = str(term).strip()
        if not t:
            continue
        try:
            conn.execute(
                "INSERT INTO dictionary_items (group_key, subkey, term) VALUES (?,?,?)",
                (group_key, subkey, t),
            )
            added += 1
        except sqlite3.IntegrityError:
            # UNIQUE 위반 = 중복
            skipped += 1
    conn.commit()
    return added, skipped


def db_remove_term(group_key: str, subkey: str, term: str) -> bool:
    """항목 삭제. 삭제된 행이 있으면 True"""
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM dictionary_items WHERE group_key=? AND subkey=? AND term=?",
        (group_key, subkey, term.strip()),
    )
    conn.commit()
    return cur.rowcount > 0


def db_replace_subkey(group_key: str, subkey: str, terms: list[str]) -> None:
    """특정 subkey 항목 전체 교체"""
    conn = _get_conn()
    conn.execute("BEGIN")
    conn.execute(
        "DELETE FROM dictionary_items WHERE group_key=? AND subkey=?",
        (group_key, subkey),
    )
    rows = [(group_key, subkey, str(t).strip()) for t in terms if str(t).strip()]
    conn.executemany(
        "INSERT OR IGNORE INTO dictionary_items (group_key, subkey, term) VALUES (?,?,?)",
        rows,
    )
    conn.execute("COMMIT")


def db_count_subkey(group_key: str, subkey: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) FROM dictionary_items WHERE group_key=? AND subkey=?",
        (group_key, subkey),
    )
    return cur.fetchone()[0]


# ── KV 스토어 (API 키 등) ────────────────────────────────────

def kv_get(key: str, default: str = "") -> str:
    conn = _get_conn()
    cur = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO kv_store (key, value, updated_at) VALUES (?,?,CURRENT_TIMESTAMP)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value),
    )
    conn.commit()


def kv_delete(key: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM kv_store WHERE key=?", (key,))
    conn.commit()
