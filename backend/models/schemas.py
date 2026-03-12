"""
Pydantic 데이터 모델 정의
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime


# ── 열거형 ────────────────────────────────────────────────────
class VerdictType(str, Enum):
    VIOLATION = "위반"
    CAUTION   = "주의"
    ALLOWED   = "허용"


class RiskLevel(str, Enum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


class DetectionType(str, Enum):
    COMPANY_NAME     = "업체명"
    REPRESENTATIVE   = "대표자명"
    PERSONNEL        = "참여인력명"
    LOGO             = "로고/CI/BI"
    EMAIL            = "이메일"
    URL              = "URL/도메인"
    BRAND            = "브랜드명"
    COLOR            = "회사 고유 색상"
    METADATA         = "메타데이터"
    OCR_TEXT         = "이미지 내 텍스트"
    INDIRECT         = "간접 식별 표현"
    WATERMARK        = "워터마크"
    BUSINESS_NUMBER  = "사업자번호"
    SLOGAN           = "슬로건/고유표현"
    UNKNOWN          = "기타"


class JobStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    FAILED     = "failed"


# ── 탐지 결과 단위 ─────────────────────────────────────────────
class DetectionResult(BaseModel):
    page_number:       int
    detection_type:    DetectionType
    detected_text:     str  = ""
    image_description: Optional[str] = None
    verdict:           VerdictType
    reason:            str
    recommendation:    str
    confidence:        float = Field(default=0.8, ge=0.0, le=1.0)
    bbox:              Optional[List[float]] = None   # [x0,y0,x1,y1] 정규화 0-1
    source:            str = "rule"   # "rule" | "ocr" | "claude" | "image"


# ── 페이지 결과 ────────────────────────────────────────────────
class PageResult(BaseModel):
    page_number:      int
    thumbnail_b64:    Optional[str] = None   # JPEG base64
    detections:       List[DetectionResult] = []
    has_violation:    bool = False
    has_caution:      bool = False
    violation_count:  int  = 0
    caution_count:    int  = 0
    allowed_count:    int  = 0

    def recalc(self):
        self.violation_count = sum(1 for d in self.detections if d.verdict == VerdictType.VIOLATION)
        self.caution_count   = sum(1 for d in self.detections if d.verdict == VerdictType.CAUTION)
        self.allowed_count   = sum(1 for d in self.detections if d.verdict == VerdictType.ALLOWED)
        self.has_violation   = self.violation_count > 0
        self.has_caution     = self.caution_count   > 0


# ── 문서 요약 ──────────────────────────────────────────────────
class DocumentSummary(BaseModel):
    no_company_name:   bool = True
    no_personnel:      bool = True
    no_email_url:      bool = True
    indirect_count:    int  = 0
    logo_detected:     bool = False
    metadata_clean:    bool = True
    notes:             List[str] = []


# ── 검증 리포트 ────────────────────────────────────────────────
class VerificationReport(BaseModel):
    job_id:                  str
    filename:                str
    total_pages:             int
    risk_level:              RiskLevel
    violation_count:         int
    caution_count:           int
    allowed_count:           int
    page_results:            List[PageResult] = []
    summary:                 DocumentSummary
    created_at:              datetime
    processing_time_seconds: float


# ── API 요청/응답 ──────────────────────────────────────────────
class JobStatusResponse(BaseModel):
    job_id:    str
    status:    JobStatus
    progress:  int   = Field(0, ge=0, le=100)
    message:   str   = ""
    report:    Optional[Dict[str, Any]] = None
    error:     Optional[str] = None


class DictionaryUpdateRequest(BaseModel):
    group:       str          # direct_identifiers | indirect_identifiers | allowed_terms
    subcategory: str
    items:       List[str]
    action:      str          # add | remove | replace
