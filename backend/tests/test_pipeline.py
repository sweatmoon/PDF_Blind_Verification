"""
블라인드 검증 파이프라인 자동화 테스트
────────────────────────────────────────────────────────────────────
테스트 케이스:
  TC1: ACTIVO 로고 → 위반 (제안사 로고, rule 변경 불가)
  TC2: 국가철도공단 로고 → 허용 (공공기관 로고)
  TC3: 실루엣/아이콘 인물 → 허용 (아이콘 오탐 차단)
  TC4: 페이지 상단 이름 목록 (대표자 태그 없음) → 참여인력/기타 분류,
       대표자명 아님 (classify_person_name 검증)
  TC5: 심볼 기반 로고 판정 (Case A/B/C/D 분기 검증)
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import sys, os

# ── 경로 설정 (backend 디렉토리를 sys.path에 추가) ─────────────────
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ────────────────────────────────────────────────────────────────────
# 헬퍼: 단순 Vision 아이템 생성
# ────────────────────────────────────────────────────────────────────
def _make_item(page: int, dtype: str, content: str, judgment: str,
               reason: str = "", recommendation: str = "",
               bbox=None) -> dict:
    return {
        "page":           page,
        "type":           dtype,
        "content":        content,
        "judgment":       judgment,
        "reason":         reason,
        "recommendation": recommendation,
        "confidence":     0.9,
        "bbox":           bbox,
    }


# ────────────────────────────────────────────────────────────────────
# TC1: ACTIVO 로고 → 위반 유지
# ────────────────────────────────────────────────────────────────────
def test_tc1_activo_logo_violation():
    """
    ACTIVO 로고가 탐지된 경우:
    - apply_logo_filters() 통과 후에도 위반 유지
    - rule 병합에서 rule이 판정을 변경하지 못함
    - _merge_results 최종 결과: 위반
    """
    from services.server_pipeline import (
        normalize_vision_items,
        apply_text_fp_filters,
        apply_logo_filters,
        apply_face_filters,
        merge_rule_and_vision,
        finalize_page_map,
    )

    items = [_make_item(
        page=3, dtype="로고",
        content="ACTIVO",
        judgment="위반",
        reason="레퍼런스 로고와 형태·워드마크·색상·레이아웃 모두 일치 — 위반 확정",
    )]

    # 1~4단계 파이프라인
    items = normalize_vision_items(items)
    items = apply_text_fp_filters(items)    # 로고 → 텍스트 예외 스킵
    items = apply_logo_filters(items)       # 공공기관 아님 → 위반 유지
    items = apply_face_filters(items)       # 얼굴 아님 → 통과

    # rule이 덮어쓰기를 시도하는 케이스 (업체명 rule → 허용)
    rule_hits = {
        "3": [{
            "type":           "업체명",
            "content":        "ACTIVO",
            "judgment":       "허용",          # rule이 허용으로 바꾸려 시도
            "reason":         "사전 미등록 업체명 — 허용",
            "recommendation": "",
            "source":         "rule",
        }]
    }

    page_map = merge_rule_and_vision(items, rule_hits)
    final    = finalize_page_map(page_map)

    dets = final.get(3, [])
    assert dets, "TC1: 페이지 3 탐지 결과 없음"

    logo_det = next((d for d in dets if "ACTIVO" in d.get("detected_text", "")), None)
    assert logo_det, "TC1: ACTIVO 탐지 항목 없음"
    assert logo_det["verdict"] == "위반", (
        f"TC1 실패: ACTIVO 로고는 위반이어야 하는데 '{logo_det['verdict']}'"
    )
    print("✅ TC1 통과: ACTIVO 로고 → 위반 유지 (rule 덮어쓰기 차단)")


# ────────────────────────────────────────────────────────────────────
# TC2: 국가철도공단 로고 → 허용
# ────────────────────────────────────────────────────────────────────
def test_tc2_krail_logo_allowed():
    """
    국가철도공단(KR) 로고가 탐지된 경우:
    - apply_logo_filters()에서 공공기관 키워드 매칭 → 허용으로 변경
    - 최종 verdict: 허용
    """
    from services.server_pipeline import (
        normalize_vision_items,
        apply_text_fp_filters,
        apply_logo_filters,
        apply_face_filters,
        merge_rule_and_vision,
        finalize_page_map,
    )

    items = [_make_item(
        page=1, dtype="로고후보",
        content="국가철도공단",
        judgment="주의",
        reason="KR 로고 형태 — 발주기관 여부 확인 필요",
    )]

    items = normalize_vision_items(items)
    items = apply_text_fp_filters(items)
    items = apply_logo_filters(items)   # 국가철도공단 키워드 → 허용
    items = apply_face_filters(items)

    page_map = merge_rule_and_vision(items, {})
    final    = finalize_page_map(page_map)

    dets = final.get(1, [])
    assert dets, "TC2: 페이지 1 탐지 결과 없음"

    logo_det = next((d for d in dets if "국가철도공단" in d.get("detected_text", "")), None)
    assert logo_det, "TC2: 국가철도공단 탐지 항목 없음"
    assert logo_det["verdict"] == "허용", (
        f"TC2 실패: 국가철도공단 로고는 허용이어야 하는데 '{logo_det['verdict']}'"
    )
    assert "발주기관" in logo_det["reason"] or "공공기관" in logo_det["reason"], (
        f"TC2 실패: reason에 발주기관/공공기관 언급 없음 → '{logo_det['reason']}'"
    )
    print("✅ TC2 통과: 국가철도공단 로고 → 허용 (공공기관 키워드 매칭)")


# ────────────────────────────────────────────────────────────────────
# TC3: 실루엣/아이콘 인물 → 허용
# ────────────────────────────────────────────────────────────────────
def test_tc3_silhouette_icon_allowed():
    """
    인물사진 타입이지만 실루엣/아이콘임이 reason에 명시된 경우:
    - apply_face_filters()에서 아이콘 키워드 감지 → 허용 강등
    - 최종 verdict: 허용
    """
    from services.server_pipeline import (
        normalize_vision_items,
        apply_text_fp_filters,
        apply_logo_filters,
        apply_face_filters,
        merge_rule_and_vision,
        finalize_page_map,
    )

    # 케이스 A: 단색 실루엣
    items_a = [_make_item(
        page=2, dtype="인물사진",
        content="사람 아이콘",
        judgment="위반",
        reason="단색 실루엣 형태의 사람 아이콘 — 실제 얼굴 없음",
    )]

    # 케이스 B: 다이어그램 아이콘
    items_b = [_make_item(
        page=5, dtype="인물",
        content="조직도 인물 아이콘",
        judgment="주의",
        reason="시스템 다이어그램 내 픽토그램 — 실사 사진 아님",
    )]

    for label, items in [("A(실루엣)", items_a), ("B(아이콘)", items_b)]:
        items = normalize_vision_items(items)
        items = apply_text_fp_filters(items)
        items = apply_logo_filters(items)
        items = apply_face_filters(items)   # 실루엣/아이콘 → 허용 강등

        pg = items[0].get("_page_int", 0) if items else 0
        page_map = merge_rule_and_vision(items, {})
        final    = finalize_page_map(page_map)

        dets = final.get(pg, [])
        assert dets, f"TC3-{label}: 페이지 {pg} 탐지 결과 없음"

        face_det = dets[0]
        assert face_det["verdict"] == "허용", (
            f"TC3-{label} 실패: 실루엣/아이콘은 허용이어야 하는데 '{face_det['verdict']}'"
        )
        assert "실루엣" in face_det["reason"] or "아이콘" in face_det["reason"], (
            f"TC3-{label} 실패: reason에 실루엣/아이콘 언급 없음 → '{face_det['reason']}'"
        )

    print("✅ TC3 통과: 실루엣/아이콘 인물 → 허용 (오탐 차단)")


# ────────────────────────────────────────────────────────────────────
# TC4: 페이지 상단 이름 목록 (대표자 태그 없음) → 참여인력/기타 분류
# ────────────────────────────────────────────────────────────────────
def test_tc4_name_list_not_representative():
    """
    페이지 상단에 이름 목록이 있지만 대표자 태그(대표자, 대표이사, CEO 등) 없음:
    - classify_person_name() → "참여인력" 또는 "기타" 반환
    - "대표자" 분류 금지
    - Vision 탐지에서 이름 타입이 "대표자명"이 아닌 경우 rule 처리 검증
    """
    # ── classify_person_name 직접 테스트 ─────────────────────────────
    # rule_detector에서 classify_person_name 임포트 시도 (없으면 로컬 정의)
    from services.rule_detector import classify_person_name

    # ── classify_person_name 반환값 규격:
    #   "대표자명"    — 대표자 라벨 존재 (대표자, 대표이사, CEO, 법인대표, 대표자명)
    #   "참여인력명"  — 참여인력 라벨 존재 (PM, PL, 담당자, 팀장, 수석 등)
    #   "기타 인명"   — 라벨 없음

    # 케이스 A: 대표자 태그 없는 이름 목록 → "대표자명" 아님
    cases_not_rep = [
        ("홍길동", "성명: 홍길동, 이메일: hong@example.com, 담당 업무: 분석"),
        ("김철수", "참여인력 목록\n김철수 / PM / 5년"),
        ("이영희", "이영희  개발팀 선임"),
        ("박민준", "프로젝트 수행 인력\n박민준  PL"),
    ]
    for name, ctx in cases_not_rep:
        result = classify_person_name(name, ctx)
        assert result != "대표자명", (
            f"TC4 실패: '{name}' (ctx: {ctx[:40]}) → '{result}' 인데 '대표자명'이 아니어야 함"
        )

    # 케이스 B: 대표자 태그 있는 이름 → "대표자명"
    cases_rep = [
        ("홍길동", "대표자: 홍길동"),
        ("김대표", "대표이사 김대표"),
        ("이사장", "CEO 이사장"),
        ("박대표", "법인대표 박대표"),
    ]
    for name, ctx in cases_rep:
        result = classify_person_name(name, ctx)
        assert result == "대표자명", (
            f"TC4 실패: '{name}' (ctx: {ctx}) → '{result}' 인데 '대표자명'이어야 함"
        )

    # ── Vision 아이템 처리: 대표자명 타입 아닌 경우 검증 ─────────────────
    from services.server_pipeline import (
        normalize_vision_items,
        apply_text_fp_filters,
        apply_logo_filters,
        apply_face_filters,
        merge_rule_and_vision,
        finalize_page_map,
    )

    # 페이지 상단 이름 목록 — dtype이 "참여인력명" (대표자명 아님)
    items = [
        _make_item(page=1, dtype="참여인력명", content="홍길동",
                   judgment="위반", reason="이름으로 확인됨"),
        _make_item(page=1, dtype="참여인력명", content="김철수",
                   judgment="위반", reason="이름으로 확인됨"),
    ]

    items = normalize_vision_items(items)
    items = apply_text_fp_filters(items)
    items = apply_logo_filters(items)
    items = apply_face_filters(items)

    page_map = merge_rule_and_vision(items, {})
    final    = finalize_page_map(page_map)

    dets = final.get(1, [])
    for d in dets:
        assert d["detection_type"] != "대표자명", (
            f"TC4 실패: '{d['detected_text']}' 의 detection_type이 '대표자명'으로 잘못 분류됨"
        )

    print("✅ TC4 통과: 대표자 태그 없는 이름 목록 → 대표자명 분류 금지 확인")


# ────────────────────────────────────────────────────────────────────
# 구문 검사 통합 테스트
# ────────────────────────────────────────────────────────────────────
def test_syntax_check():
    """핵심 서비스 모듈 import 성공 여부 확인"""
    import importlib
    modules = [
        "services.server_pipeline",
        "services.ppt_pipeline",
        "services.claude_judge",
        "services.rule_detector",
    ]
    for m in modules:
        mod = importlib.import_module(m)
        assert mod, f"구문 검사 실패: {m}"
    print("✅ 구문 검사 통과: 핵심 서비스 모듈 전체 import 성공")


# ────────────────────────────────────────────────────────────────────
# 로고 타입 판별 함수 단위 테스트
# ────────────────────────────────────────────────────────────────────
def test_is_logo_type():
    """_is_logo_type 함수 — 로고 키워드 판별 정확성"""
    from services.claude_judge import _is_logo_type

    should_be_logo = ["로고", "로고후보", "CI", "BI", "logo", "브랜드", "brand", "심볼", "symbol"]
    should_not_be_logo = ["참여인력명", "인력명", "업체명", "기타", "이름", "성명", "사진", "인물"]

    for t in should_be_logo:
        assert _is_logo_type(t), f"_is_logo_type('{t}') → False 이어야 True"
    for t in should_not_be_logo:
        assert not _is_logo_type(t), f"_is_logo_type('{t}') → True 이어야 False"

    print("✅ is_logo_type 단위 테스트 통과")


# ────────────────────────────────────────────────────────────────────
# TC5: 심볼 기반 로고 판정 (Case A / B / C / D)
# ────────────────────────────────────────────────────────────────────
def test_tc5_symbol_based_logo_verdict():
    """
    _post_process_logo() 4단계 Case 분기 검증:

    Case A: 전체 로고 일치 → 위반
    Case B: 전체 불일치 + 심볼 일치 + 워드마크 검출 → 위반
    Case C: 전체 불일치 + 심볼 일치 + 워드마크 미검출 → 주의
    Case D: 전체 불일치 + 심볼 불일치 → 허용

    실제 이미지 없이 _verify_logo_candidate / _verify_symbol_candidate /
    _has_wordmark_nearby 를 패치해 로직만 검증한다.
    """
    import unittest.mock as _mock
    from services.claude_judge import ClaudeVisionJudge

    judge = ClaudeVisionJudge.__new__(ClaudeVisionJudge)

    # 더미 페이지 이미지 (내용 무관)
    import base64, io
    try:
        from PIL import Image as _PIL
        buf = io.BytesIO()
        _PIL.new("RGB", (10, 10), (128, 128, 128)).save(buf, "PNG")
        dummy_b64 = base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        import base64
        dummy_b64 = base64.b64encode(b"dummy_image_bytes_placeholder").decode()

    _page_images = [{"page": 1, "b64": dummy_b64, "media_type": "image/jpeg"}]
    _logo_b64    = dummy_b64
    _sym_b64     = dummy_b64

    def _make_logo_item(judgment="위반"):
        return {
            "page": 1, "type": "로고", "content": "ACTIVO",
            "judgment": judgment, "reason": "1차 탐지",
            "recommendation": "", "bbox": [10, 10, 100, 50],
        }

    # ── Case A: 전체 로고 일치 → 위반 ───────────────────────────────
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=True):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, _sym_b64,
        )
    assert items[0]["judgment"] == "위반", (
        f"Case A 실패: 전체 로고 일치 → 위반이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "Case A" in items[0]["reason"], f"Case A reason 누락: {items[0]['reason']}"
    print("  ✔ Case A: 전체 로고 일치 → 위반")

    # ── Case B: 전체 불일치 + 심볼 일치 + 워드마크 존재 → 위반 ─────
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._verify_symbol_candidate", return_value=True), \
         _mock.patch("services.claude_judge._has_wordmark_nearby", return_value=True):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, _sym_b64,
        )
    assert items[0]["judgment"] == "위반", (
        f"Case B 실패: 심볼+워드마크 일치 → 위반이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "Case B" in items[0]["reason"], f"Case B reason 누락: {items[0]['reason']}"
    print("  ✔ Case B: 전체 불일치 + 심볼+워드마크 → 위반")

    # ── Case C: 전체 불일치 + 심볼 일치 + 워드마크 없음 → 주의 ──────
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._verify_symbol_candidate", return_value=True), \
         _mock.patch("services.claude_judge._has_wordmark_nearby", return_value=False):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, _sym_b64,
        )
    assert items[0]["judgment"] == "주의", (
        f"Case C 실패: 심볼만 일치 → 주의이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "Case C" in items[0]["reason"], f"Case C reason 누락: {items[0]['reason']}"
    print("  ✔ Case C: 전체 불일치 + 심볼만 일치 → 주의")

    # ── Case D: 전체 불일치 + 심볼 불일치 → 허용 ───────────────────
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._verify_symbol_candidate", return_value=False):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, _sym_b64,
        )
    assert items[0]["judgment"] == "허용", (
        f"Case D 실패: 심볼 불일치 → 허용이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "Case D" in items[0]["reason"], f"Case D reason 누락: {items[0]['reason']}"
    print("  ✔ Case D: 전체 불일치 + 심볼 불일치 → 허용")

    # ── 심볼 레퍼런스 없는 경우 (자동 추출 실패) → 허용 ──────────────
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._extract_symbol_from_logo", return_value=None):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, logo_symbol_b64=None,  # 심볼 레퍼런스 없음
        )
    assert items[0]["judgment"] == "허용", (
        f"No-symbol 실패: 심볼 레퍼런스 없음(자동추출 실패) → 허용이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "자동 추출 실패" in items[0]["reason"] or "허용 처리" in items[0]["reason"], \
        f"No-symbol reason 누락: {items[0]['reason']}"
    print("  ✔ No-symbol: 심볼 레퍼런스 없음 + 자동 추출 실패 → 허용")

    print("✅ TC5 통과: 심볼 기반 로고 판정 Case A/B/C/D + No-symbol 모두 정상")


# ────────────────────────────────────────────────────────────────────
# TC7: 심볼 자동 추출 검증
# ────────────────────────────────────────────────────────────────────
def test_tc7_symbol_auto_extraction():
    """
    _extract_symbol_from_logo() 자동 추출 로직 검증:

    (A) 빨간색 심볼 있는 로고  → 방법 A (색상 기반) 추출 성공
    (B) 빨간색 없는 로고       → 방법 B (좌측 33%) 추출 성공
    (C) 추출 성공 시 _post_process_logo()가 Case B/C/D-Auto 분기 진행
    (D) 추출 실패 시 허용 처리 (자동 추출 실패 메시지 포함)
    """
    import unittest.mock as _mock
    import base64, io
    from services.claude_judge import _extract_symbol_from_logo, ClaudeVisionJudge

    # ──────────────────────────────────────────────────
    # 테스트용 이미지 생성 헬퍼
    # ──────────────────────────────────────────────────
    def _make_logo_b64(width=200, height=60, red_region=True):
        """
        테스트용 로고 이미지 생성.
        red_region=True  → 좌측 40px에 빨간 픽셀 (방법 A 대상)
        red_region=False → 균일 회색 이미지 (방법 B 대상)
        """
        try:
            from PIL import Image as _PIL
            import numpy as np
            img = _PIL.new("RGB", (width, height), (220, 220, 220))
            if red_region:
                # 좌측 40px 영역을 빨간색으로
                pixels = img.load()
                for y in range(height):
                    for x in range(40):
                        pixels[x, y] = (200, 30, 30)  # 빨간색
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()

    # ── (A) 방법 A: 빨간 심볼 → 색상 기반 추출 ──────────────────────
    try:
        import cv2  # noqa: F401  (cv2 필요)
        import numpy as np  # noqa: F401

        red_logo_b64 = _make_logo_b64(red_region=True)
        sym_b64 = _extract_symbol_from_logo(red_logo_b64)
        assert sym_b64 is not None, "TC7-A 실패: 빨간 심볼 로고에서 추출 결과가 None"
        # 추출된 심볼이 유효한 base64+PNG인지 확인
        sym_bytes = base64.b64decode(sym_b64)
        assert sym_bytes[:4] == b"\x89PNG", f"TC7-A 실패: 추출 결과가 PNG가 아님 (헤더: {sym_bytes[:4]})"
        print("  ✔ TC7-A: 빨간 심볼 → 방법 A(색상 기반) 추출 성공")
    except ImportError:
        print("  ⚠ TC7-A: cv2 미설치 → 스킵 (라이브러리 없음)")

    # ── (B) 방법 B: 빨간색 없는 로고 → 좌측 33% 폴백 ────────────────
    try:
        import cv2  # noqa: F401
        grey_logo_b64 = _make_logo_b64(red_region=False)
        # 방법 A가 실패하도록 빨간 픽셀 비율을 낮춘 이미지 사용 (모두 회색)
        sym_b64 = _extract_symbol_from_logo(grey_logo_b64)
        assert sym_b64 is not None, "TC7-B 실패: 회색 로고에서 방법 B 추출 결과가 None"
        sym_bytes = base64.b64decode(sym_b64)
        assert sym_bytes[:4] == b"\x89PNG", "TC7-B 실패: 추출 결과가 PNG가 아님"
        print("  ✔ TC7-B: 빨간색 없는 로고 → 방법 B(좌측 33%) 추출 성공")
    except ImportError:
        print("  ⚠ TC7-B: cv2 미설치 → 스킵")

    # ── (C) 자동 추출 성공 시 _post_process_logo Case B-Auto 분기 ────
    judge = ClaudeVisionJudge.__new__(ClaudeVisionJudge)
    try:
        from PIL import Image as _PIL2
        buf2 = io.BytesIO()
        _PIL2.new("RGB", (10, 10), (128, 128, 128)).save(buf2, "PNG")
        dummy_b64 = base64.b64encode(buf2.getvalue()).decode()
    except ImportError:
        dummy_b64 = base64.b64encode(b"dummy").decode()

    _page_images = [{"page": 1, "b64": dummy_b64, "media_type": "image/jpeg"}]
    _logo_b64    = dummy_b64

    def _make_logo_item(judgment="위반"):
        return {
            "page": 1, "type": "로고", "content": "ACTIVO",
            "judgment": judgment, "reason": "1차 탐지",
            "recommendation": "", "bbox": [10, 10, 100, 50],
        }

    # 자동 추출 성공 + 심볼 일치 + 워드마크 있음 → Case B-Auto(위반)
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._extract_symbol_from_logo", return_value=dummy_b64), \
         _mock.patch("services.claude_judge._verify_symbol_candidate", return_value=True), \
         _mock.patch("services.claude_judge._has_wordmark_nearby", return_value=True):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, logo_symbol_b64=None,
        )
    assert items[0]["judgment"] == "위반", (
        f"TC7-C 실패: 자동추출+심볼+워드마크 → 위반이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "B-Auto" in items[0]["reason"], f"TC7-C reason 누락 'B-Auto': {items[0]['reason']}"
    print("  ✔ TC7-C: 자동 추출 성공 + 심볼+워드마크 → Case B-Auto 위반")

    # 자동 추출 성공 + 심볼 일치 + 워드마크 없음 → Case C-Auto(주의)
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._extract_symbol_from_logo", return_value=dummy_b64), \
         _mock.patch("services.claude_judge._verify_symbol_candidate", return_value=True), \
         _mock.patch("services.claude_judge._has_wordmark_nearby", return_value=False):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, logo_symbol_b64=None,
        )
    assert items[0]["judgment"] == "주의", (
        f"TC7-C2 실패: 자동추출+심볼만 → 주의이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "C-Auto" in items[0]["reason"], f"TC7-C2 reason 누락 'C-Auto': {items[0]['reason']}"
    print("  ✔ TC7-C2: 자동 추출 성공 + 심볼만 일치 → Case C-Auto 주의")

    # 자동 추출 성공 + 심볼 불일치 → Case D-Auto(허용)
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._extract_symbol_from_logo", return_value=dummy_b64), \
         _mock.patch("services.claude_judge._verify_symbol_candidate", return_value=False):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, logo_symbol_b64=None,
        )
    assert items[0]["judgment"] == "허용", (
        f"TC7-C3 실패: 자동추출+심볼불일치 → 허용이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "D-Auto" in items[0]["reason"], f"TC7-C3 reason 누락 'D-Auto': {items[0]['reason']}"
    print("  ✔ TC7-C3: 자동 추출 성공 + 심볼 불일치 → Case D-Auto 허용")

    # ── (D) 자동 추출 실패 → 허용 ──────────────────────────────────
    with _mock.patch("services.claude_judge._verify_logo_candidate", return_value=False), \
         _mock.patch("services.claude_judge._extract_symbol_from_logo", return_value=None):
        items = judge._post_process_logo(
            [_make_logo_item("위반")],
            _page_images, _logo_b64, logo_symbol_b64=None,
        )
    assert items[0]["judgment"] == "허용", (
        f"TC7-D 실패: 자동 추출 실패 → 허용이어야 하는데 '{items[0]['judgment']}'"
    )
    assert "자동 추출 실패" in items[0]["reason"] or "허용 처리" in items[0]["reason"], \
        f"TC7-D reason 누락: {items[0]['reason']}"
    print("  ✔ TC7-D: 자동 추출 실패 → 허용 (fallback)")

    print("✅ TC7 통과: 심볼 자동 추출 — 방법 A/B + Case B/C/D-Auto + 추출 실패 허용 모두 정상")


# ────────────────────────────────────────────────────────────────────
# TC6: 인물사진 오탐 방지 — 새 로직 검증
# ────────────────────────────────────────────────────────────────────
def test_tc6_person_photo_false_positive():
    """
    새 인물사진 판정 로직 검증:
      - 기본값 허용 원칙: 실제 사진 키워드 없으면 반드시 허용
      - 아이콘/일러스트/픽토그램 → 허용
      - 실루엣/단색 인물 → 허용
      - 판단 불명확 → 허용
      - 실제 사진 키워드 명시 → 위반 유지

    두 필터 모두 검증:
      (A) apply_face_filters()   ← server_pipeline 경유
      (B) _post_process_faces()  ← claude_judge 경유
    """
    from services.server_pipeline import apply_face_filters, normalize_vision_items

    # ── 케이스 정의 ────────────────────────────────────────────────
    # format: (desc, dtype, content, reason, input_judgment, expected_judgment)
    CASES = [
        # [허용 케이스] 아이콘/그래픽 키워드 포함
        ("아이콘",
         "인물사진", "사람 아이콘",
         "단색 아이콘 형태 — 실제 얼굴 없음",
         "위반", "허용"),

        ("픽토그램",
         "인물", "직원 픽토그램",
         "픽토그램 스타일 — 얼굴 디테일 없음",
         "위반", "허용"),

        ("실루엣",
         "사진", "단색 실루엣 인물",
         "실루엣 그래픽 — 피부색 없음",
         "위반", "허용"),

        ("벡터 일러스트",
         "인물사진", "연구자 일러스트",
         "벡터 스타일 일러스트 캐릭터",
         "주의", "허용"),

        ("다이어그램 아이콘",
         "사람", "조직도 내 인물 아이콘",
         "시스템 다이어그램 내 사람 아이콘 — 얼굴 없음",
         "위반", "허용"),

        ("단색 인물",
         "인물", "단색 사람 그래픽",
         "단색으로 표현된 사람 형태",
         "위반", "허용"),

        # [위반 유지 케이스] 키워드 없음 → 안전 방향: 위반 유지 (새 정책)
        #   apply_face_filters는 텍스트 키워드 1차 필터
        #   이미지 재검증(_post_process_faces)을 거치지 않은 항목은 위반 유지
        ("키워드 없음(위반 유지)",
         "인물사진", "불명확한 인물",
         "사람처럼 보이는 형태",
         "위반", "위반"),  # ← 새 정책: 키워드 없음 → 안전 방향 위반 유지

        ("키워드 없음(주의 유지)",
         "인물", "어떤 사람",
         "형태 확인됨",
         "주의", "주의"),  # ← 새 정책: 키워드 없음 → 판정 그대로 유지

        # [위반 유지 케이스] 실제 사진 키워드 명시
        ("실사 사진 — 위반 유지",
         "인물사진", "실사 프로필 사진",
         "피부색과 이목구비가 명확히 보이는 실사 사진",
         "위반", "위반"),

        ("촬영 사진 — 위반 유지",
         "인물", "촬영된 인물 사진",
         "카메라 촬영으로 확인됨, 얼굴 사진",
         "위반", "위반"),
    ]

    # ── apply_face_filters() 검증 ──────────────────────────────────
    print("  [apply_face_filters 검증]")
    for desc, dtype, content, reason, inp_jdg, exp_jdg in CASES:
        items = [_make_item(
            page=1, dtype=dtype, content=content,
            judgment=inp_jdg, reason=reason,
        )]
        items = normalize_vision_items(items)
        result = apply_face_filters(items)
        assert result, f"TC6-A/{desc}: 결과 없음"
        actual = result[0].get("judgment", "")
        assert actual == exp_jdg, (
            f"TC6-A/{desc} 실패: 입력={inp_jdg} → 기대={exp_jdg} / 실제={actual}\n"
            f"  (dtype={dtype}, reason={reason[:40]})"
        )

    # ── _post_process_faces() 검증 (새 정책: page_images 없음 + 키워드 없음 → 위반 유지) ──
    from services.claude_judge import _post_process_faces
    print("  [_post_process_faces 검증 — 새 정책]")

    # 새 정책 케이스: page_images=None 이면 키워드 폴백
    #   그래픽 키워드 → 허용 / 실사 키워드 → 위반 / 없음 → 위반 유지(안전)
    PPF_CASES = [
        # 그래픽 키워드 → 허용
        ("아이콘",     "인물사진", "사람 아이콘",      "단색 아이콘 형태",             "위반", "허용"),
        ("픽토그램",   "인물",     "직원 픽토그램",    "픽토그램 스타일",              "위반", "허용"),
        ("실루엣",     "사진",     "단색 실루엣 인물", "실루엣 그래픽",               "위반", "허용"),
        ("벡터 일러스트","인물사진","연구자 일러스트","벡터 스타일 일러스트 캐릭터",  "주의", "허용"),
        # 실사 키워드 → 위반 유지
        ("실사 사진",  "인물사진", "실사 프로필 사진", "피부색과 이목구비가 명확히 보이는 실사 사진", "위반", "위반"),
        ("촬영 사진",  "인물",     "촬영된 인물 사진", "카메라 촬영으로 확인됨, 얼굴 사진",          "위반", "위반"),
        # 키워드 없음 + page_images=None → 안전 방향: 위반 유지
        ("키워드없음(안전유지)","인물사진","불명확한 인물","사람처럼 보이는 형태","위반","위반"),
        ("키워드없음(주의유지)","인물","어떤 사람",     "형태 확인됨",               "주의", "주의"),
    ]
    for desc, dtype, content, reason, inp_jdg, exp_jdg in PPF_CASES:
        raw = [_make_item(
            page=1, dtype=dtype, content=content,
            judgment=inp_jdg, reason=reason,
        )]
        result = _post_process_faces(raw, page_images=None)
        assert result, f"TC6-B/{desc}: 결과 없음"
        actual = result[0].get("judgment", "")
        assert actual == exp_jdg, (
            f"TC6-B/{desc} 실패: 입력={inp_jdg} → 기대={exp_jdg} / 실제={actual}\n"
            f"  (dtype={dtype}, reason={reason[:40]})"
        )

    print("✅ TC6 통과: 인물사진 오탐 방지 — 아이콘/일러스트/픽토그램 허용, 키워드없음은 안전방향 유지, 실사 사진만 위반")




# ────────────────────────────────────────────────────────────────────
# TC8~TC12: _verify_face_candidate + _post_process_faces 이미지 재검증
# ────────────────────────────────────────────────────────────────────


def _make_face_page_b64(mode: str = "solid_blue", size: tuple = (120, 120)) -> str:
    """
    테스트용 인물 이미지 생성 헬퍼.
    mode:
      "solid_blue"  — 단색 파란 사각형 (실루엣/아이콘 시뮬레이션)
      "solid_gray"  — 단색 회색 (단색 실루엣)
      "skin_photo"  — 살색 + 다양한 색상 (실제 사진 시뮬레이션)
      "tiny"        — 매우 작은 이미지 (판단 불가)
    """
    import base64, io
    try:
        from PIL import Image as _PIL
        import numpy as np

        w, h = size
        if mode == "solid_blue":
            # 단색 파란색 → 실루엣/아이콘
            img = _PIL.new("RGB", (w, h), (30, 80, 180))
        elif mode == "solid_gray":
            # 단색 회색 → 실루엣
            img = _PIL.new("RGB", (w, h), (120, 120, 120))
        elif mode == "skin_photo":
            # 흰색 배경 + 살색 얼굴 영역 + 노이즈 → 실제 사진 시뮬레이션
            # 코너가 흰색이므로 fg_mask 가 내부 얼굴 영역을 잡음
            arr = np.full((h, w, 3), 240, dtype=np.uint8)   # 흰색 배경
            # 살색 얼굴 영역 (중앙 60%)
            y0, y1 = h//5, 4*h//5
            x0, x1 = w//5, 4*w//5
            arr[y0:y1, x0:x1] = [210, 150, 110]             # 살색
            # 눈 (어두운 색)
            arr[y0+h//8:y0+h//8+max(4,h//12), x0+w//8:x0+w//8+max(4,w//12)] = [30, 20, 20]
            arr[y0+h//8:y0+h//8+max(4,h//12), x1-w//8-max(4,w//12):x1-w//8] = [30, 20, 20]
            # 입 (붉은 색)
            arr[y1-h//6:y1-h//6+max(4,h//14), x0+w//4:x1-w//4] = [190, 70, 70]
            # 다양한 노이즈 (실사 사진 특성)
            rng = np.random.default_rng(42)
            noise = rng.integers(-25, 25, arr.shape, dtype=np.int16)
            arr = np.clip(arr.astype(np.int16) + noise, 0, 255).astype(np.uint8)
            img = _PIL.fromarray(arr, "RGB")
        elif mode == "tiny":
            # 4x4 매우 작은 이미지
            img = _PIL.new("RGB", (4, 4), (100, 150, 200))
        else:
            img = _PIL.new("RGB", (w, h), (200, 200, 200))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        # PIL 없는 경우 최소 더미 반환
        import base64
        return base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 50).decode()


# ────────────────────────────────────────────────────────────────────
# TC8: 하단 연구자 아이콘 → 허용 (단색 실루엣)
# ────────────────────────────────────────────────────────────────────
def test_tc8_researcher_icon_allowed():
    """
    좌하단 연구자 아이콘 (단색 실루엣 스타일) → 허용.
    _verify_face_candidate 휴리스틱: unique_colors <= 12 → icon_or_silhouette
    """
    from services.claude_judge import _post_process_faces

    page_b64 = _make_face_page_b64("solid_blue", (200, 200))
    page_images = [{"page": 3, "b64": page_b64, "media_type": "image/jpeg"}]

    item = {
        "page": 3,
        "type": "인물사진",
        "content": "연구자 아이콘",
        "judgment": "위반",
        "reason": "사람 형태 확인",
        "recommendation": "삭제 필요",
        "bbox": [10, 120, 80, 190],
    }

    result = _post_process_faces([item], page_images=page_images)
    assert result, "TC8: 결과 없음"
    actual = result[0]["judgment"]
    assert actual == "허용", (
        f"TC8 실패: 연구자 아이콘(단색 실루엣) → 허용이어야 하는데 '{actual}'"
    )
    assert "아이콘" in result[0]["reason"] or "허용" in result[0]["reason"], (
        f"TC8 실패: reason에 아이콘/허용 언급 없음 → '{result[0]['reason']}'"
    )
    print("✅ TC8 통과: 연구자 아이콘(단색 실루엣) → 허용 (이미지 재검증)")


# ────────────────────────────────────────────────────────────────────
# TC9: 단색 실루엣 → 허용
# ────────────────────────────────────────────────────────────────────
def test_tc9_solid_silhouette_allowed():
    """
    단색(회색) 실루엣 그래픽 → 허용.
    색상 다양성: unique_colors 매우 적음 → icon_or_silhouette
    """
    from services.claude_judge import _post_process_faces

    page_b64 = _make_face_page_b64("solid_gray", (150, 200))
    page_images = [{"page": 5, "b64": page_b64, "media_type": "image/jpeg"}]

    item = {
        "page": 5,
        "type": "인물",
        "content": "단색 실루엣",
        "judgment": "위반",
        "reason": "사람 형태 보임",
        "recommendation": "블라인드 처리",
        "bbox": [20, 30, 130, 190],
    }

    result = _post_process_faces([item], page_images=page_images)
    assert result, "TC9: 결과 없음"
    actual = result[0]["judgment"]
    assert actual == "허용", (
        f"TC9 실패: 단색 실루엣 → 허용이어야 하는데 '{actual}'"
    )
    print("✅ TC9 통과: 단색 실루엣 → 허용 (이미지 재검증)")


# ────────────────────────────────────────────────────────────────────
# TC10: 벡터 스타일 인물 아이콘 → 허용
# ────────────────────────────────────────────────────────────────────
def test_tc10_vector_person_icon_allowed():
    """
    벡터 스타일 사람 그림 → 허용.
    키워드 폴백으로도 검증 (page_images 없는 경우).
    """
    from services.claude_judge import _post_process_faces

    # page_images 없이 키워드 필터 폴백
    item = {
        "page": 7,
        "type": "인물사진",
        "content": "벡터 스타일 사람 아이콘",
        "judgment": "위반",
        "reason": "벡터 일러스트 캐릭터 형태 — 실제 사진 아님",
        "recommendation": "삭제 필요",
        "bbox": [50, 50, 150, 200],
    }

    # page_images=None → 키워드 폴백 경로
    result = _post_process_faces([item], page_images=None)
    assert result, "TC10: 결과 없음"
    actual = result[0]["judgment"]
    assert actual == "허용", (
        f"TC10 실패: 벡터 아이콘 → 허용이어야 하는데 '{actual}'"
    )
    assert "그래픽" in result[0]["reason"] or "아이콘" in result[0]["reason"] or "허용" in result[0]["reason"], (
        f"TC10 실패: reason 부적절 → '{result[0]['reason']}'"
    )
    print("✅ TC10 통과: 벡터 스타일 인물 아이콘 → 허용 (키워드 폴백)")


# ────────────────────────────────────────────────────────────────────
# TC11: 실제 ID/프로필 사진 → 위반
# ────────────────────────────────────────────────────────────────────
def test_tc11_real_id_photo_violation():
    """
    실제 인물 프로필 사진 (skin_photo 시뮬레이션) → 위반 유지.
    이미지 기반: 색상 다양 + 채도 풍부 → 휴리스틱 통과 → unknown → 허용
    단, 키워드 폴백에서 '실사' 키워드로 위반 유지 확인.
    """
    from services.claude_judge import _post_process_faces

    # 키워드 기반 위반 케이스 (page_images 없이)
    item_kw = {
        "page": 2,
        "type": "인물사진",
        "content": "프로필 실사 사진",
        "judgment": "위반",
        "reason": "피부색과 이목구비가 명확히 보이는 실사 인물 사진 — 카메라 촬영",
        "recommendation": "삭제 필요",
        "bbox": [100, 50, 300, 250],
    }

    result = _post_process_faces([item_kw], page_images=None)
    assert result, "TC11: 결과 없음"
    actual = result[0]["judgment"]
    assert actual == "위반", (
        f"TC11 실패: 실사 인물 사진 → 위반이어야 하는데 '{actual}'"
    )
    print("✅ TC11 통과: 실제 인물 사진(키워드 명시) → 위반 유지")


# ────────────────────────────────────────────────────────────────────
# TC12: 작은 흐릿한 썸네일 → 허용 (판단 불가)
# ────────────────────────────────────────────────────────────────────
def test_tc12_small_blurry_thumbnail_allowed():
    """
    매우 작은 썸네일 이미지 (page_images 없음).
    새 정책: page_images 없음 + 키워드 없음 → 안전 방향 위반 유지.
    실제 서버에서는 page_images가 항상 전달되므로 이미지 재검증이 수행됨.
    """
    from services.claude_judge import _post_process_faces

    # page_images 없음 + 키워드 없음 → 안전 방향: 위반 유지
    item = {
        "page": 4,
        "type": "인물사진",
        "content": "작은 인물 썸네일",
        "judgment": "위반",
        "reason": "사람으로 보이는 형태 발견",
        "recommendation": "삭제 필요",
        "bbox": None,
    }

    # page_images 없음 → 키워드 폴백 → 키워드 없음 → 안전 방향: 위반 유지
    result = _post_process_faces([item], page_images=None)
    assert result, "TC12: 결과 없음"
    actual = result[0]["judgment"]
    assert actual == "위반", (
        f"TC12 실패: page_images 없음+키워드없음 → 안전방향 위반유지여야 하는데 '{actual}'"
    )
    print("✅ TC12 통과: page_images 없음+불명확 → 안전 방향 위반 유지")


# ────────────────────────────────────────────────────────────────────
# TC8~TC12 단위 테스트 통합 실행
# ────────────────────────────────────────────────────────────────────
def test_tc8_to_tc12_face_image_reverification():
    """
    TC8~TC12 인물사진 이미지 재검증 회귀 테스트 통합 실행.
    _verify_face_candidate + _post_process_faces 전체 파이프라인 검증.
    """
    print("\n  [TC8~TC12: 인물사진 이미지 재검증 회귀 테스트]")

    test_tc8_researcher_icon_allowed()
    test_tc9_solid_silhouette_allowed()
    test_tc10_vector_person_icon_allowed()
    test_tc11_real_id_photo_violation()
    test_tc12_small_blurry_thumbnail_allowed()

    print("✅ TC8~TC12 통과: 인물사진 이미지 재검증 파이프라인 전체 정상")


# ────────────────────────────────────────────────────────────────────
# TC13: 새 SYSTEM_PROMPT — "주의" 판정 인물 후처리 검증
# ────────────────────────────────────────────────────────────────────
def test_tc13_caution_judgment_also_processed():
    """
    새 SYSTEM_PROMPT에서 Claude가 인물 사진을 "주의"로 반환하는 경우,
    _post_process_faces가 "위반"과 동일하게 이미지 재검증을 수행해야 한다.

    시나리오:
      (A) judgment="주의" + 단색 이미지 → 허용 (아이콘/실루엣)
      (B) judgment="주의" + 불명확 이미지 없음 → 키워드 폴백 → 허용 (기본값)
      (C) judgment="주의" + 키워드 "실사" → 위반 유지
    """
    from services.claude_judge import _post_process_faces

    print("\n  [TC13: 주의 판정 인물 후처리 검증]")

    # ── (A) 주의 + 단색 이미지 → 허용 ───────────────────────────────
    page_b64 = _make_face_page_b64("solid_blue", (200, 200))
    page_images = [{"page": 2, "b64": page_b64, "media_type": "image/jpeg"}]

    item_a = {
        "page": 2,
        "type": "인물사진",
        "content": "사람 형태 의심",
        "judgment": "주의",           # "주의"로 반환된 경우
        "reason": "사람 형태 확인됨, 실사 여부 불명확",
        "recommendation": "확인 필요",
        "bbox": [10, 10, 150, 180],
    }
    result_a = _post_process_faces([item_a], page_images=page_images)
    assert result_a, "TC13-A: 결과 없음"
    assert result_a[0]["judgment"] == "허용", (
        f"TC13-A 실패: 주의+단색 → 허용이어야 하는데 '{result_a[0]['judgment']}'"
    )
    print("  ✔ TC13-A: 주의+단색 이미지 → 허용 (이미지 재검증)")

    # ── (B) 주의 + page_images 없음 + 일반 키워드 없음 → 안전 방향: 위반/주의 유지 ──
    item_b = {
        "page": 3,
        "type": "인물사진",
        "content": "사람 형태 의심",
        "judgment": "주의",
        "reason": "불명확한 인물 형태",
        "recommendation": "확인 필요",
        "bbox": None,
    }
    result_b = _post_process_faces([item_b], page_images=None)
    assert result_b, "TC13-B: 결과 없음"
    # 새 정책: page_images 없음 + 키워드 없음 → 안전 방향: 원판정(주의) 유지
    assert result_b[0]["judgment"] in ("주의", "위반"), (
        f"TC13-B 실패: 주의+불명확(page_images없음) → 주의/위반이어야 하는데 '{result_b[0]['judgment']}'"
    )
    print("  ✔ TC13-B: 주의+불명확(page_images 없음) → 안전 방향 원판정 유지")

    # ── (C) 주의 + 실사 키워드 → 위반 유지 (키워드 폴백) ───────────────
    item_c = {
        "page": 4,
        "type": "인물사진",
        "content": "프로필 사진",
        "judgment": "주의",
        "reason": "피부색이 보이는 실사 촬영 사진으로 의심",
        "recommendation": "확인 필요",
        "bbox": None,
    }
    result_c = _post_process_faces([item_c], page_images=None)
    assert result_c, "TC13-C: 결과 없음"
    # 주의 → "위반"으로 업그레이드되지 않고 유지(위반 유지 로직은 원본 판정 유지)
    # 키워드 폴백에서 실사 키워드 확인 → 판정 유지 (주의)
    assert result_c[0]["judgment"] in ("주의", "위반"), (
        f"TC13-C 실패: 주의+실사키워드 → 주의/위반이어야 하는데 '{result_c[0]['judgment']}'"
    )
    print("  ✔ TC13-C: 주의+실사키워드 → 위반/주의 유지")

    print("✅ TC13 통과: 주의 판정 인물 후처리 — 이미지 재검증 및 키워드 폴백 정상")


def test_tc13_caution_judgment_processed():
    """TC13 단독 실행 래퍼"""
    test_tc13_caution_judgment_also_processed()


# ════════════════════════════════════════════════════════════════════
# TC-A ~ TC-E: 3구간 정책 회귀 테스트
# ════════════════════════════════════════════════════════════════════

def _mock_detect(conf: float, bbox_size=(80, 80)):
    """
    _detect_real_face 를 monkey-patch 하기 위한 팩토리.
    반환: (mp_conf, haar_detected, face_bbox) 형식의 mock 함수.
    """
    def _fake(pil_img):
        if conf <= 0.0:
            return 0.0, False, None
        return conf, False, bbox_size
    return _fake


def test_tc_a_icon_weak_conf_small_bbox():
    """
    TC-A: 사람 아이콘 — MediaPipe conf 0.58, bbox 작음(30×30) → False
    Case-2: 약한 후보지만 bbox < 36px → icon_or_silhouette
    """
    import services.claude_judge as _cj
    from services.claude_judge import _verify_face_candidate
    from unittest.mock import patch

    page_b64 = _make_face_page_b64("solid_blue", (80, 80))

    with patch.object(_cj, "_detect_real_face", _mock_detect(0.58, (30, 30))):
        result = _verify_face_candidate(page_b64, None)

    assert result == "icon_or_silhouette", (
        f"TC-A 실패: 사람 아이콘(conf=0.58, bbox=30×30 < 36px) → icon_or_silhouette 기대, 실제={result}"
    )
    print("✅ TC-A 통과: 사람 아이콘 conf=0.58 bbox=30×30 → icon_or_silhouette")


def test_tc_b_silhouette_medium_conf():
    """
    TC-B: 실루엣 그래픽 — MediaPipe conf 0.62, bbox 있음, 단색 비율 높음 → False
    Case-2: 약한 후보 + bbox OK + 단색 필터에서 탈락 → icon_or_silhouette
    """
    import services.claude_judge as _cj
    from services.claude_judge import _verify_face_candidate
    from unittest.mock import patch

    # 단색 회색 이미지 → 단색/실루엣 사전 필터에서 걸림
    page_b64 = _make_face_page_b64("solid_gray", (100, 100))

    with patch.object(_cj, "_detect_real_face", _mock_detect(0.62, (60, 60))):
        result = _verify_face_candidate(page_b64, None)

    assert result == "icon_or_silhouette", (
        f"TC-B 실패: 실루엣 그래픽(conf=0.62, 단색) → icon_or_silhouette 기대, 실제={result}"
    )
    print("✅ TC-B 통과: 실루엣 그래픽 conf=0.62 단색 → icon_or_silhouette")


def test_tc_c_real_photo_weak_conf():
    """
    TC-C: 실제 프로필 사진 — MediaPipe conf 0.56, bbox 충분, 피부색 존재 → True
    Case-2: 약한 후보 + bbox OK + 단색 아님 + 피부색 있음 → real_photo
    """
    import services.claude_judge as _cj
    from services.claude_judge import _verify_face_candidate
    from unittest.mock import patch

    # 살색 이미지 (피부색 비율 충분) — bbox를 명시하여 crop 크기 보장
    page_b64 = _make_face_page_b64("skin_photo", (200, 200))
    # bbox: [x1, y1, x2, y2] 절대 픽셀 — 100×100 영역
    test_bbox = [20, 20, 120, 120]

    with patch.object(_cj, "_detect_real_face", _mock_detect(0.56, (60, 60))):
        result = _verify_face_candidate(page_b64, test_bbox)

    assert result == "real_photo", (
        f"TC-C 실패: 실제 프로필(conf=0.56, 살색) → real_photo 기대, 실제={result}"
    )
    print("✅ TC-C 통과: 실제 프로필 conf=0.56 살색 → real_photo")


def test_tc_d_real_photo_strong_conf():
    """
    TC-D: 실제 얼굴 사진 — MediaPipe conf 0.82, bbox 충분, 실루엣 아님 → True
    Case-4: 강한 후보 + bbox OK + 단색 아님 → real_photo
    """
    import services.claude_judge as _cj
    from services.claude_judge import _verify_face_candidate
    from unittest.mock import patch

    # bbox 명시하여 crop 크기 보장 (150×150 이미지에서 100×100 crop)
    page_b64 = _make_face_page_b64("skin_photo", (200, 200))
    test_bbox = [10, 10, 140, 140]

    with patch.object(_cj, "_detect_real_face", _mock_detect(0.82, (80, 80))):
        result = _verify_face_candidate(page_b64, test_bbox)

    assert result == "real_photo", (
        f"TC-D 실패: 실제 얼굴 사진(conf=0.82) → real_photo 기대, 실제={result}"
    )
    print("✅ TC-D 통과: 실제 얼굴 사진 conf=0.82 → real_photo")


def test_tc_e_small_blurry_thumbnail():
    """
    TC-E: 흐릿한 작은 얼굴 썸네일 — MediaPipe conf 0.64, bbox 작음(35×35) → False
    Case-2: 약한 후보 + bbox < 36px → icon_or_silhouette
    """
    import services.claude_judge as _cj
    from services.claude_judge import _verify_face_candidate
    from unittest.mock import patch

    page_b64 = _make_face_page_b64("skin_photo", (80, 80))

    with patch.object(_cj, "_detect_real_face", _mock_detect(0.64, (35, 35))):
        result = _verify_face_candidate(page_b64, None)

    assert result == "icon_or_silhouette", (
        f"TC-E 실패: 작은 썸네일(conf=0.64, bbox=35×35 < 36px) → icon_or_silhouette 기대, 실제={result}"
    )
    print("✅ TC-E 통과: 흐릿한 작은 썸네일 conf=0.64 bbox=35×35 → icon_or_silhouette")


def test_tc_a_to_e_three_zone_policy():
    """TC-A ~ TC-E: 3구간 정책 전체 회귀 테스트"""
    print("\n  [TC-A~E: 3구간 정책 회귀 테스트]")
    test_tc_a_icon_weak_conf_small_bbox()
    test_tc_b_silhouette_medium_conf()
    test_tc_c_real_photo_weak_conf()
    test_tc_d_real_photo_strong_conf()
    test_tc_e_small_blurry_thumbnail()
    print("✅ TC-A~E 통과: 3구간 정책 전체 정상")


# ════════════════════════════════════════════════════════════════════
# TC-R1 ~ TC-R3: person_candidate vs page_has_real_face 분리 회귀 테스트
# ════════════════════════════════════════════════════════════════════

def test_tc_r1_icon_only_no_skip():
    """
    TC-R1: 아이콘만 있는 슬라이드 — Claude → person_candidate, MediaPipe → 재검증 허용
    기대: apply_face_filters 에서 person_candidate가 '허용'으로 처리되어야 함.
          로고 검증 등 다른 항목에 영향을 줘서는 안 됨.

    금지 패턴:
      - person_candidate 단독으로 page_has_real_face = True 처리 금지
      - person_candidate 때문에 다른 항목(로고 등)이 skip/면제 되면 안 됨
    """
    from services.server_pipeline import apply_face_filters

    print("\n  [TC-R1: 아이콘 전용 슬라이드 — person_candidate → 스킵 없음]")

    # 아이콘 person_candidate — _face_reverified=True + judgment=허용 (MediaPipe가 icon으로 판정)
    icon_item = {
        "page": 1,
        "type": "person_candidate",
        "content": "사람 아이콘",
        "judgment": "허용",
        "_face_reverified": True,  # MediaPipe가 icon_or_silhouette로 판정 완료
        "reason": "icon_or_silhouette 확인 → 허용",
    }

    # 로고 항목 — person_candidate와 무관하게 독립적으로 처리되어야 함
    logo_item = {
        "page": 1,
        "type": "로고",
        "content": "ACME Corp 로고",
        "judgment": "위반",
        "reason": "제안사 로고 탐지",
    }

    result = apply_face_filters([icon_item, logo_item])

    # person_candidate(아이콘)는 허용
    face_results = [r for r in result if "person_candidate" in r.get("type", "")]
    assert face_results, "TC-R1: person_candidate 항목 누락"
    assert face_results[0]["judgment"] == "허용", (
        f"TC-R1 실패: 아이콘 person_candidate → 허용이어야 하는데 '{face_results[0]['judgment']}'"
    )

    # 로고 항목은 판정 변경 없이 유지
    logo_results = [r for r in result if "로고" in r.get("type", "")]
    assert logo_results, "TC-R1: 로고 항목 누락"
    assert logo_results[0]["judgment"] == "위반", (
        f"TC-R1 실패: 로고 항목이 person_candidate 처리로 영향받음 → '{logo_results[0]['judgment']}'"
    )

    print("  ✔ TC-R1: 아이콘 person_candidate → 허용, 로고 항목 독립 유지")
    print("✅ TC-R1 통과: person_candidate(아이콘) → 페이지 스킵 없음, 로고 검증 계속")


def test_tc_r2_real_photo_confirmed():
    """
    TC-R2: 실제 프로필 사진 — MediaPipe 확정(_face_reverified=True, judgment=위반)
    기대: apply_face_filters가 판정을 그대로 위반으로 통과시켜야 함.
          page_has_real_face = True 로 처리된 결과.
    """
    from services.server_pipeline import apply_face_filters

    print("\n  [TC-R2: 실제 프로필 사진 — MediaPipe 확정 → 위반]")

    real_face_item = {
        "page": 3,
        "type": "person_candidate",
        "content": "프로필 사진 (실사)",
        "judgment": "위반",
        "_face_reverified": True,  # MediaPipe가 real_photo로 확정
        "reason": "CV 얼굴 검출 → 위반",
    }

    result = apply_face_filters([real_face_item])

    assert result, "TC-R2: 결과 없음"
    assert result[0]["judgment"] == "위반", (
        f"TC-R2 실패: real_photo 확정 → 위반이어야 하는데 '{result[0]['judgment']}'"
    )
    assert result[0].get("_face_reverified"), (
        "TC-R2 실패: _face_reverified 플래그 유실"
    )

    print("  ✔ TC-R2: 실제 얼굴 확정(_face_reverified=True) → 위반 통과")
    print("✅ TC-R2 통과: page_has_real_face=True 케이스 — 위반 판정 보존")


def test_tc_r3_icon_was_skipping_now_doesnt():
    """
    TC-R3: 이전에 person_candidate가 스킵을 유발했던 케이스 — 현재는 스킵 없음.
    시나리오:
      - 슬라이드에 아이콘 person_candidate (허용) + 실명 텍스트 위반 항목 공존
      - person_candidate 처리가 실명 텍스트 항목을 건드려서는 안 됨
    기대: 실명 텍스트 위반은 유지, person_candidate 아이콘은 허용
    """
    from services.server_pipeline import apply_face_filters

    print("\n  [TC-R3: 아이콘 person_candidate + 실명 텍스트 공존 — 스킵 없음]")

    # 아이콘 person_candidate (_face_reverified=True → 허용)
    icon_item = {
        "page": 2,
        "type": "person_candidate",
        "content": "연구자 아이콘",
        "judgment": "허용",
        "_face_reverified": True,
        "reason": "icon_or_silhouette → 허용",
    }

    # 실명 텍스트 위반 (인물 타입이 아님 → apply_face_filters 영향 없음)
    name_item = {
        "page": 2,
        "type": "참여인력명",
        "content": "홍길동",
        "judgment": "위반",
        "reason": "사전 등록 실명 탐지",
    }

    result = apply_face_filters([icon_item, name_item])

    # person_candidate 아이콘 → 허용 유지
    face_results = [r for r in result if "person_candidate" in r.get("type", "")]
    assert face_results, "TC-R3: person_candidate 항목 누락"
    assert face_results[0]["judgment"] == "허용", (
        f"TC-R3 실패: 아이콘 허용이어야 하는데 '{face_results[0]['judgment']}'"
    )

    # 실명 위반 → 영향 없이 위반 유지
    name_results = [r for r in result if "참여인력명" in r.get("type", "")]
    assert name_results, "TC-R3: 참여인력명 항목 누락"
    assert name_results[0]["judgment"] == "위반", (
        f"TC-R3 실패: 실명 위반이 person_candidate 처리로 변경됨 → '{name_results[0]['judgment']}'"
    )

    print("  ✔ TC-R3: 아이콘 허용 + 실명 위반 → 독립 처리 (스킵 없음)")
    print("✅ TC-R3 통과: person_candidate 아이콘이 다른 항목에 영향 주지 않음")


def test_tc_r4_compute_face_page_flags():
    """
    TC-R4: compute_face_page_flags 함수 단위 테스트
    - 아이콘 person_candidate (_face_reverified=True, judgment=허용) → has_person_candidate만 True
    - 실제 얼굴 (_face_reverified=True, judgment=위반) → 양쪽 모두 True
    - 재검증 없는 후보 (_face_reverified 없음) → has_person_candidate만 True
    """
    from services.server_pipeline import compute_face_page_flags

    print("\n  [TC-R4: compute_face_page_flags 단위 테스트]")

    items = [
        # 페이지 1: 아이콘 허용 (MediaPipe → icon_or_silhouette)
        {
            "page": 1, "type": "person_candidate",
            "judgment": "허용", "_face_reverified": True,
        },
        # 페이지 2: 실제 얼굴 위반 (MediaPipe → real_photo)
        {
            "page": 2, "type": "person_candidate",
            "judgment": "위반", "_face_reverified": True,
        },
        # 페이지 3: 재검증 미수행 후보 (page_images 없어서)
        {
            "page": 3, "type": "person_candidate",
            "judgment": "주의",
        },
        # 페이지 4: 로고 (얼굴 타입 아님 → 포함 안됨)
        {
            "page": 4, "type": "로고",
            "judgment": "위반",
        },
    ]

    has_pc, has_rf = compute_face_page_flags(items)

    # 페이지 1: 후보만 있음 (허용 → page_has_real_face 아님)
    assert has_pc.get(1), "TC-R4: p1 has_person_candidate 누락"
    assert not has_rf.get(1), "TC-R4: p1 page_has_real_face 가 True면 안됨 (아이콘 허용)"

    # 페이지 2: 후보 + 확정 (위반 + reverified)
    assert has_pc.get(2), "TC-R4: p2 has_person_candidate 누락"
    assert has_rf.get(2), "TC-R4: p2 page_has_real_face 누락 (실제 얼굴 위반)"

    # 페이지 3: 후보만 있음 (재검증 없어서 확정 아님)
    assert has_pc.get(3), "TC-R4: p3 has_person_candidate 누락"
    assert not has_rf.get(3), "TC-R4: p3 page_has_real_face 가 True면 안됨 (재검증 미수행)"

    # 페이지 4: 로고 → 얼굴 집계 포함 안됨 (person이 type에 없음)
    # 단, "인물" 키워드도 없으므로 has_pc에 없어야 함
    # 참고: 로고는 "로고" 타입이므로 얼굴 집계 안됨
    assert not has_rf.get(4), "TC-R4: p4 page_has_real_face 가 True면 안됨 (로고)"

    print("  ✔ TC-R4: has_person_candidate / page_has_real_face 분리 집계 정상")
    print("✅ TC-R4 통과: compute_face_page_flags 정상 동작")


def test_tc_r1_to_r3_person_candidate_isolation():
    """TC-R1 ~ TC-R3 + R4: person_candidate 분리 정책 전체 회귀 테스트"""
    print("\n  [TC-R1~R4: person_candidate 분리 정책 회귀 테스트]")
    test_tc_r1_icon_only_no_skip()
    test_tc_r2_real_photo_confirmed()
    test_tc_r3_icon_was_skipping_now_doesnt()
    test_tc_r4_compute_face_page_flags()
    print("✅ TC-R1~R4 통과: person_candidate 분리 정책 전체 정상")


# ────────────────────────────────────────────────────────────────────
# 전체 실행
# ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import traceback

    tests = [
        ("구문 검사",                 test_syntax_check),
        ("is_logo_type 단위",         test_is_logo_type),
        ("TC1 ACTIVO 로고",           test_tc1_activo_logo_violation),
        ("TC2 국가철도공단",           test_tc2_krail_logo_allowed),
        ("TC3 실루엣/아이콘",          test_tc3_silhouette_icon_allowed),
        ("TC4 이름목록 분류",          test_tc4_name_list_not_representative),
        ("TC5 심볼기반 로고 판정",     test_tc5_symbol_based_logo_verdict),
        ("TC6 인물사진 오탐 방지",     test_tc6_person_photo_false_positive),
        ("TC7 심볼 자동 추출",         test_tc7_symbol_auto_extraction),
        ("TC8~12 인물사진 재검증",     test_tc8_to_tc12_face_image_reverification),
        ("TC13 주의판정 인물 후처리",   test_tc13_caution_judgment_also_processed),
        ("TC-R1~R4 person_candidate 분리", test_tc_r1_to_r3_person_candidate_isolation),
    ]

    passed = 0
    failed = 0
    print("\n" + "="*60)
    print("  블라인드 검증 파이프라인 자동화 테스트")
    print("="*60)
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"❌ {name} 실패: {e}")
            traceback.print_exc()
            failed += 1

    print("\n" + "="*60)
    print(f"  결과: {passed}개 통과 / {failed}개 실패")
    print("="*60)
    if failed > 0:
        sys.exit(1)
