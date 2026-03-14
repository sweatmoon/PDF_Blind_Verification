"""
블라인드 검증 파이프라인 자동화 테스트
────────────────────────────────────────────────────────────────────
테스트 케이스:
  TC1: ACTIVO 로고 → 위반 (제안사 로고, rule 변경 불가)
  TC2: 국가철도공단 로고 → 허용 (공공기관 로고)
  TC3: 실루엣/아이콘 인물 → 허용 (아이콘 오탐 차단)
  TC4: 페이지 상단 이름 목록 (대표자 태그 없음) → 참여인력/기타 분류,
       대표자명 아님 (classify_person_name 검증)
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
# 전체 실행
# ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import traceback

    tests = [
        ("구문 검사",          test_syntax_check),
        ("is_logo_type 단위",  test_is_logo_type),
        ("TC1 ACTIVO 로고",    test_tc1_activo_logo_violation),
        ("TC2 국가철도공단",   test_tc2_krail_logo_allowed),
        ("TC3 실루엣/아이콘",  test_tc3_silhouette_icon_allowed),
        ("TC4 이름목록 분류",  test_tc4_name_list_not_representative),
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
