"""
분석 로그 모듈 — 검증 전 과정을 job_id별 JSON 파일로 기록

로그 파일: logs/analysis/<job_id>.jsonl
포맷: 각 줄이 독립적인 JSON 이벤트 (JSON Lines)

이벤트 구조:
{
  "ts":    "2025-03-14T12:34:56.789",   # 타임스탬프
  "job":   "abc123",                     # job_id
  "stage": "face_verify",               # 파이프라인 단계
  "event": "skin_heuristic",            # 세부 이벤트명
  "data":  { ... }                      # 단계별 세부 데이터
}

주요 stage 목록:
  pipeline          — 전체 파이프라인 흐름 (시작/종료/단계별 소요시간)
  rule_detect       — 규칙 기반 텍스트 탐지
  ocr               — OCR 처리
  claude_request    — Claude Vision API 요청
  claude_response   — Claude Vision API 응답 (원문 + 파싱 결과)
  logo_postprocess  — 로고 후처리 (pHash/SSIM/ORB 수치 포함)
  face_postprocess  — 인물 후보 후처리 (bbox별 판정 흐름)
  face_verify       — _verify_face_candidate 내부 단계별 수치
                      (skin_fg, sat_std, mediapipe 신뢰도 등)
  server_pipeline   — server_pipeline 필터 적용 결과
  final_result      — 최종 위반/주의/허용 항목 요약
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.config import LOGS_DIR

# ── 분석 로그 디렉터리 ────────────────────────────────────────────
_ANALYSIS_DIR = LOGS_DIR / "analysis"
_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

# ── 스레드 로컬: 현재 활성 job_id ────────────────────────────────
_local = threading.local()
_lock  = threading.Lock()

# ── 파일 핸들 캐시 (job_id → file handle) ──────────────────────
_handles: dict[str, Any] = {}


def _get_handle(job_id: str):
    """job_id에 대한 로그 파일 핸들을 반환 (없으면 생성)"""
    if job_id not in _handles:
        with _lock:
            if job_id not in _handles:
                path = _ANALYSIS_DIR / f"{job_id}.jsonl"
                _handles[job_id] = open(path, "a", encoding="utf-8", buffering=1)
    return _handles[job_id]


def set_job(job_id: str):
    """현재 스레드의 활성 job_id 설정"""
    _local.job_id = job_id


def get_job() -> Optional[str]:
    """현재 스레드의 활성 job_id 반환"""
    return getattr(_local, "job_id", None)


def log(
    stage: str,
    event: str,
    data: dict,
    job_id: Optional[str] = None,
):
    """
    분석 이벤트 1건을 JSONL 파일에 기록.

    Args:
        stage:  파이프라인 단계명 (예: "face_verify", "logo_postprocess")
        event:  세부 이벤트명 (예: "skin_heuristic", "phash_match")
        data:   단계별 세부 데이터 dict
        job_id: 명시적 job_id (None이면 스레드 로컬에서 가져옴)
    """
    jid = job_id or get_job()
    if not jid:
        return  # job_id 없으면 무시

    entry = {
        "ts":    datetime.now().isoformat(timespec="milliseconds"),
        "job":   jid,
        "stage": stage,
        "event": event,
        "data":  data,
    }
    try:
        fh = _get_handle(jid)
        with _lock:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 로그 실패가 서비스에 영향 주지 않도록


def close_job(job_id: str):
    """job 완료 후 파일 핸들 닫기"""
    with _lock:
        fh = _handles.pop(job_id, None)
        if fh:
            try:
                fh.close()
            except Exception:
                pass


def get_log_path(job_id: str) -> Path:
    """job_id의 분석 로그 파일 경로 반환"""
    return _ANALYSIS_DIR / f"{job_id}.jsonl"


def read_log(job_id: str) -> list[dict]:
    """분석 로그 전체를 파싱하여 이벤트 리스트로 반환"""
    path = get_log_path(job_id)
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def summarize_log(job_id: str) -> dict:
    """
    분석 로그를 단계별로 요약.
    반환 형식:
    {
      "job_id": "...",
      "total_events": N,
      "stages": {
        "pipeline":       [...],
        "claude_request": [...],
        "face_verify":    [...],
        ...
      }
    }
    """
    events = read_log(job_id)
    stages: dict[str, list] = {}
    for ev in events:
        s = ev.get("stage", "unknown")
        stages.setdefault(s, []).append(ev)
    return {
        "job_id":       job_id,
        "total_events": len(events),
        "stages":       stages,
    }
