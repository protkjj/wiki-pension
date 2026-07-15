"""플랫폼 데이터 계층 테스트: 인증, 회사/제출/결과 CRUD, 워크플로우."""

import pytest

from dbo.platform import auth, seed, store
from dbo.platform.db import init_db

NOW = "2025-12-31T00:00:00"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "platform.sqlite"
    init_db(p)
    return str(p)


# --- 인증 ------------------------------------------------------------------


def test_password_hash_roundtrip():
    h, salt = auth.hash_password("secret")
    assert auth.verify_password("secret", h, salt)
    assert not auth.verify_password("wrong", h, salt)
    # 평문이 저장되지 않음
    assert "secret" not in h


def test_authenticate_success_and_failure(db):
    cid = auth.create_company(db, "가나전자", NOW)
    auth.create_user(db, "clientA", "pw123", "client", NOW, company_id=cid)

    ok = auth.authenticate(db, "clientA", "pw123")
    assert ok is not None
    assert ok["role"] == "client"
    assert ok["company_name"] == "가나전자"

    assert auth.authenticate(db, "clientA", "nope") is None
    assert auth.authenticate(db, "ghost", "pw123") is None


# --- 시드 ------------------------------------------------------------------


def test_seed_creates_demo_once(db):
    assert seed.seed_if_empty(db, NOW) is True
    assert seed.seed_if_empty(db, NOW) is False   # 두 번째는 생성 안 함
    assert auth.authenticate(db, "admin", "admin123")["role"] == "admin"
    assert auth.authenticate(db, "actuary", "act123")["role"] == "actuary"


# --- 워크플로우: 제출 → 상태 전이 → 결과 -----------------------------------


def test_submission_workflow(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)

    # 오류 있는 업로드 → needs_fix
    sid = store.create_submission(db, cid, uid, "census.xlsx", "/tmp/x.xlsx",
                                  "2025-12-31", "needs_fix", 100, 3, 5, NOW)
    assert store.get_submission(db, sid)["status"] == "needs_fix"

    # 수정 후 검증완료
    store.update_submission_status(db, sid, "validated", NOW)
    assert store.get_submission(db, sid)["status"] == "validated"

    # 제출
    store.update_submission_status(db, sid, "submitted", NOW)
    subs = store.list_submissions(db, statuses=["submitted"])
    assert len(subs) == 1 and subs[0]["id"] == sid

    # 계리인 계산 결과 저장
    aid = auth.create_user(db, "act", "pw", "actuary", NOW)
    store.save_result(db, sid, aid, 1.23e10, 4.5e8, 95, 5, "/tmp/r.xlsx", "/tmp/log.json", NOW)
    store.update_submission_status(db, sid, "calculated", NOW)
    res = store.latest_result(db, sid)
    assert res["total_dbo"] == pytest.approx(1.23e10)

    # 회사별 필터
    assert len(store.list_submissions(db, company_id=cid)) == 1
    assert store.status_counts(db)["calculated"] == 1


def test_stage_mapping_and_on_hold():
    # 내부 status → 고객 단계 라벨 (신청 → 접수완료 → 계산완료 → 기업검토요청 → 보고완료)
    assert store.stage_of("needs_fix") == "신청"
    assert store.stage_of("validated") == "신청"
    assert store.stage_of("submitted") == "신청"
    assert store.stage_of("accepted") == "접수완료"
    assert store.stage_of("calculated") == "계산완료"
    assert store.stage_of("client_review") == "기업검토요청"
    assert store.stage_of("on_hold") == "보류중"
    assert store.stage_of("reported") == "보고완료"


def test_workflow_calc_report_versioning(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "accepted", 10, 0, 0, NOW)
    # 계산완료 → 버전 증가·완료일시 기록
    v1 = store.mark_calculated(db, sid, NOW)
    assert v1 == 1
    s = store.get_submission(db, sid)
    assert s["status"] == "calculated" and s["calculated_at"] == NOW and s["calc_version"] == 1
    # 재계산 → 버전 2
    assert store.mark_calculated(db, sid, NOW) == 2
    # 기업검토요청 → 기업 확인 → 최종 보고완료
    store.update_submission_status(db, sid, "client_review", NOW)
    store.mark_client_confirmed(db, sid, NOW)
    assert store.get_submission(db, sid)["client_confirmed_at"] == NOW
    store.mark_reported(db, sid, NOW)
    s = store.get_submission(db, sid)
    assert s["status"] == "reported" and s["reported_at"] == NOW
    # 5개 단계가 모두 정의됨
    assert store.STAGE_LABELS == ["신청", "접수완료", "계산완료", "기업검토요청", "보고완료"]
    assert "on_hold" in store.STATUS_LABELS


def test_audit_comment_roundtrip(db):
    cid = auth.create_company(db, "코멘트사", NOW)
    uid = auth.create_user(db, "clientC", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "client_review", 10, 0, 0, NOW)
    assert store.get_submission(db, sid)["audit_comment"] is None
    store.set_audit_comment(db, sid, "기초율·할인율 가정 설명 코멘트", NOW)
    assert store.get_submission(db, sid)["audit_comment"] == "기초율·할인율 가정 설명 코멘트"


def test_recalc_resets_client_review(db):
    # 기업검토완료 후 재계산 → 검토완료·검토요청 이력이 초기화되고 계산완료로 되돌아간다.
    cid = auth.create_company(db, "라진테크", NOW)
    uid = auth.create_user(db, "clientR", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "accepted", 10, 0, 0, NOW)
    store.mark_calculated(db, sid, NOW)
    store.update_submission_status(db, sid, "client_review", NOW)
    store.stamp_stage_time(db, sid, "review_requested_at", NOW)
    store.mark_client_confirmed(db, sid, NOW)
    s = store.get_submission(db, sid)
    assert s["status"] == "client_review" and s["client_confirmed_at"] == NOW
    # 재계산 → 검토상태 초기화
    store.mark_calculated(db, sid, NOW)
    s2 = store.get_submission(db, sid)
    assert s2["status"] == "calculated"
    assert s2["client_confirmed_at"] is None
    assert s2["review_requested_at"] is None


def test_submission_purpose_applicant_and_meta(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "validated", 10, 0, 0, NOW,
                                  purpose="재정검증", applicant="가나전자 인사담당")
    s = store.get_submission(db, sid)
    assert s["purpose"] == "재정검증"
    assert s["applicant"] == "가나전자 인사담당"
    assert s["calculator"] is None
    # 신청 시 산출자·목적·견적액·비고 갱신
    store.set_submission_meta(db, sid, purpose="IFRS-1019", calculator="위키소프트",
                              quote_amount=150.0, note="우선순위 높음")
    s = store.get_submission(db, sid)
    assert s["purpose"] == "IFRS-1019" and s["calculator"] == "위키소프트"
    assert s["quote_amount"] == pytest.approx(150.0)
    assert s["note"] == "우선순위 높음"


def test_acceptance_of_and_result_metrics(db):
    assert store.acceptance_of("submitted") == "신청"
    assert store.acceptance_of("accepted") == "접수"
    assert store.acceptance_of("on_hold") == "보류"
    assert store.acceptance_of("cancelled") == "취소"
    assert "cancelled" in store.STATUS_LABELS

    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    aid = auth.create_user(db, "act", "pw", "actuary", NOW)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "calculated", 10, 0, 0, NOW)
    rid = store.save_result(db, sid, aid, 6.1e9, 4.2e8, 10, 0, "/tmp/r.xlsx", "/tmp/l.json", NOW)
    store.save_result_metrics(db, rid, {"pbo": 6.1e9, "duration": 8.4, "prior_pbo": 5.5e9})
    res = store.latest_result(db, sid)
    import json
    m = json.loads(res["metrics_json"])
    assert m["duration"] == pytest.approx(8.4)
    assert m["prior_pbo"] == pytest.approx(5.5e9)


def test_sales_partial_save_merges(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "admin", "pw", "admin", NOW)
    store.save_sales(db, cid, {"contact_name": "홍길동", "settlement_month": "12월",
                               "address": "서울"}, uid, NOW)
    # 일부 필드만 저장해도 나머지는 유지
    store.save_sales(db, cid, {"contact_phone": "02-1234"}, uid, NOW)
    sv = store.get_sales(db, cid)
    assert sv["settlement_month"] == "12월"
    assert sv["address"] == "서울"
    assert sv["contact_phone"] == "02-1234"


def test_delete_permissions_by_role_and_stage(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "submitted", 10, 0, 0, NOW)
    # 신청(제출) 단계: 기업 수정·삭제 가능
    assert store.can_client_modify("submitted") is True
    # 계리사 접수 후: 기업은 수정·삭제 불가, 계리사는 삭제 가능(보고완료 전)
    store.update_submission_status(db, sid, "accepted", NOW)
    assert store.can_client_modify("accepted") is False
    assert store.can_actuary_delete("accepted") is True
    # 보고완료(reported): 아무도 삭제 불가
    store.update_submission_status(db, sid, "reported", NOW)
    assert store.can_actuary_delete("reported") is False
    assert store.delete_submission(db, sid) is False
    assert store.get_submission(db, sid) is not None
    # 접수 단계로 되돌리면 삭제 가능(계리사)
    store.update_submission_status(db, sid, "accepted", NOW)
    assert store.delete_submission(db, sid) is True
    assert store.get_submission(db, sid) is None


def test_find_open_submission_dedup(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "validated", 10, 0, 0, NOW)
    # 같은 기준일의 접수 전 건은 찾아짐(재업로드 대상)
    found = store.find_open_submission(db, cid, "2025-12-31")
    assert found and found["id"] == sid
    # 다른 기준일은 없음
    assert store.find_open_submission(db, cid, "2024-12-31") is None
    # 접수 후에는 재업로드 대상 아님(신규 취급)
    store.update_submission_status(db, sid, "accepted", NOW)
    assert store.find_open_submission(db, cid, "2025-12-31") is None


def test_submission_billing_and_promised_date(db):
    cid = auth.create_company(db, "라진전자", NOW)
    uid = auth.create_user(db, "c", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2026-01-01", "accepted", 10, 0, 0, NOW)
    store.set_submission_meta(db, sid, quote_amount=330.0, promised_date="2026-03-15",
                              quote_sent="2026-01-01", contract_sent="2026-03-01",
                              collection_status="미수")
    s = store.get_submission(db, sid)
    assert s["quote_amount"] == pytest.approx(330.0)
    assert s["promised_date"] == "2026-03-15"
    assert s["quote_sent"] == "2026-01-01"
    assert s["contract_sent"] == "2026-03-01"
    assert s["collection_status"] == "미수"


def test_qa_threaded_questions(db):
    cid = auth.create_company(db, "라진전자", NOW)
    uid = auth.create_user(db, "c", "pw", "client", NOW, company_id=cid)
    aid = auth.create_user(db, "act", "pw", "actuary", NOW, display_name="이기훈")
    q1 = store.add_question(db, cid, uid, "산출기준일 문의", "기준일이 언제인가요?", NOW)
    q2 = store.add_question(db, cid, uid, "할인율 문의", "할인율 근거는?", NOW)
    assert sorted(t["qno"] for t in store.list_qa_threads(db, cid)) == [1, 2]
    store.add_answer(db, cid, aid, q1, "12월 31일입니다.", NOW)
    threads = {t["id"]: t for t in store.list_qa_threads(db, cid)}
    assert len(threads[q1]["answers"]) == 1
    assert threads[q1]["answers"][0]["body"] == "12월 31일입니다."
    assert len(threads[q2]["answers"]) == 0
    rows = {r["title"]: r for r in store.qa_question_rows(db)}
    assert rows["산출기준일 문의"]["answered"] != "-"
    assert rows["산출기준일 문의"]["answered_by"] == "이기훈"
    assert rows["할인율 문의"]["answered"] == "-"


def test_aux_census_and_other_lt(db):
    cid = auth.create_company(db, "라진전자", NOW)
    uid = auth.create_user(db, "c", "pw", "client", NOW, company_id=cid)
    # 보조 명부(퇴직자 등)
    aid = store.add_aux_census(db, cid, "퇴직자 및 DC전환명부", "ret.xlsx", "/tmp/ret.xlsx", uid, NOW)
    store.add_aux_census(db, cid, "전출입명부", "tr.xlsx", "/tmp/tr.xlsx", uid, NOW)
    assert len(store.list_aux_census(db, cid)) == 2
    assert len(store.list_aux_census(db, cid, "퇴직자 및 DC전환명부")) == 1
    store.delete_aux_census(db, aid)
    assert len(store.list_aux_census(db, cid)) == 1
    # 기타장기 규정
    import json
    store.save_other_lt(db, cid, json.dumps([{"근속년수": "10년", "지급액": "금 5돈"}],
                                            ensure_ascii=False), "비고", uid, NOW)
    olt = store.get_other_lt(db, cid)
    assert json.loads(olt["rules_json"])[0]["지급액"] == "금 5돈"
    assert olt["note"] == "비고"


def test_plan_detail_and_funding_status(db):
    import json
    cid = auth.create_company(db, "라진전자", NOW)
    uid = auth.create_user(db, "c", "pw", "client", NOW, company_id=cid)
    # 제도 상세(JSON) 저장 + 부분저장 병합
    store.save_plan_info(db, cid, {"benefit_rule": "누진제", "retirement_age": 60,
                                   "detail_json": json.dumps({"under1yr_method": "월할(절상)",
                                                              "discount_basis": "회사채AA+"})},
                         uid, NOW)
    store.save_plan_info(db, cid, {"funding_ratio": 80.0}, uid, NOW)   # 부분 저장
    pi = store.get_plan_info(db, cid)
    assert pi["benefit_rule"] == "누진제"          # 유지
    assert pi["funding_ratio"] == 80.0
    d = json.loads(pi["detail_json"])
    assert d["under1yr_method"] == "월할(절상)"    # detail 유지

    # 사외적립자산 현황
    store.save_funding_status(db, cid, "2025-12-31",
                              json.dumps({"기초잔액": 1e9, "기말_퇴직연금": 1.2e9}), uid, NOW)
    fs = store.get_funding_status(db, cid)
    assert fs["valuation_date"] == "2025-12-31"
    assert json.loads(fs["data_json"])["기초잔액"] == 1e9


def test_rate_deletion_guard(db):
    import json
    cid = auth.create_company(db, "가드전자", NOW)
    uid = auth.create_user(db, "u", "pw", "actuary", NOW)
    sid = store.create_submission(db, cid, uid, "r.xlsx", "/tmp/r.xlsx",
                                  "2025-12-31", "submitted", 5, 0, 0, NOW)
    # 할인율 커브 2개
    c_used = store.add_discount_curve(db, "202512 AA0", "202512", "AA0", "",
                                      json.dumps([{"maturity": 1, "rate": 0.03}]),
                                      None, None, "", uid, NOW)
    c_free = store.add_discount_curve(db, "202412 AA0", "202412", "AA0", "",
                                      json.dumps([{"maturity": 1, "rate": 0.04}]),
                                      None, None, "", uid, NOW)
    # 기초율 세트 2개
    s_used = store.add_base_rate_set(db, "개발원2512", "개발원", "2512", "당기",
                                     60, 0.03, json.dumps([]), "", uid, NOW)
    s_free = store.add_base_rate_set(db, "개발원2412", "개발원", "2412", "전기",
                                     60, 0.03, json.dumps([]), "", uid, NOW)
    # 결과에 c_used·s_used 사용 기록
    rid = store.save_result(db, sid, uid, 1e9, 1e8, 5, 0, "/tmp/x.xlsx", "/tmp/l.json", NOW)
    store.set_result_rate_refs(db, rid, s_used, c_used)

    assert store.discount_curve_in_use(db, c_used) is True
    assert store.discount_curve_in_use(db, c_free) is False
    assert store.base_rate_set_in_use(db, s_used) is True
    assert store.base_rate_set_in_use(db, s_free) is False


def test_census_templates():
    from dbo import census_templates as CT
    assert list(CT.CENSUS_TYPES.keys())[0] == "재직자명부"
    assert len(CT.CENSUS_TYPES) == 5
    b = CT.build_template(CT.CENSUS_TYPES["재직자명부"]["cols"],
                          CT.CENSUS_TYPES["재직자명부"]["guide"], "재직자명부")
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(b))
    assert "작성요령" in wb.sheetnames          # 작성요령 시트는 양식에 포함(안내용)
    assert "사원번호" in [c.value for c in wb["재직자명부"][1]]

    # 작성요령 시트가 함께 있어도 업로드 시엔 명부(데이터) 시트만 읽는다.
    import tempfile
    import os
    from dbo import census
    ws = wb["재직자명부"]
    ws.append(["E001", "19900101", 1, "20200101", 3000000,
               None, None, 1, None, None, None])
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        wb.save(tf.name)
        tmp = tf.name
    try:
        df = census._read_dataframe(tmp)
        assert "사원번호" in list(df.columns)     # 작성요령이 아니라 명부 헤더
        assert len(df) == 1
    finally:
        os.unlink(tmp)


def test_update_submission_file(db):
    cid = auth.create_company(db, "라진전자", NOW)
    uid = auth.create_user(db, "c", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "old.xlsx", "/tmp/old.xlsx",
                                  "2025-12-31", "accepted", 100, 5, 3, NOW)
    store.update_submission_file(db, sid, "fixed.xlsx", "/tmp/fixed.xlsx", 98, 0, 1, NOW)
    s = store.get_submission(db, sid)
    assert s["filename"] == "fixed.xlsx"
    assert s["stored_path"] == "/tmp/fixed.xlsx"
    assert s["n_records"] == 98 and s["n_errors"] == 0 and s["n_warnings"] == 1
    assert s["status"] == "accepted"      # 상태는 유지


def test_qa_pending_counts(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    aid = auth.create_user(db, "act", "pw", "actuary", NOW)
    q1 = store.add_question(db, cid, uid, "제목1", "질문1", NOW)
    store.add_question(db, cid, uid, "제목2", "질문2", NOW)
    assert store.qa_pending_counts(db).get(cid) == 2
    # q1에 답하면 대기 1
    store.add_answer(db, cid, aid, q1, "답변", NOW)
    assert store.qa_pending_counts(db).get(cid) == 1


def test_hold_and_resume(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c.xlsx", "/tmp/c.xlsx",
                                  "2025-12-31", "submitted", 10, 0, 0, NOW)
    store.update_submission_status(db, sid, "on_hold", NOW)
    assert store.stage_of(store.get_submission(db, sid)["status"]) == "보류중"
    store.update_submission_status(db, sid, "accepted", NOW)
    assert store.stage_of(store.get_submission(db, sid)["status"]) == "접수완료"


def test_plan_info_upsert(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    assert store.get_plan_info(db, cid) is None

    store.save_plan_info(db, cid, {
        "plan_type": "DB(확정급여)", "retirement_age": 60, "salary_basis": "평균임금",
        "interim_allowed": 1, "funding_ratio": 80.0, "notes": "초기 등록",
    }, uid, NOW)
    pi = store.get_plan_info(db, cid)
    assert pi["plan_type"] == "DB(확정급여)"
    assert pi["retirement_age"] == 60

    # 같은 회사 재저장 → 갱신(중복 생성 아님)
    store.save_plan_info(db, cid, {"plan_type": "DC(확정기여)", "retirement_age": 62}, uid, NOW)
    pi2 = store.get_plan_info(db, cid)
    assert pi2["plan_type"] == "DC(확정기여)"
    assert pi2["retirement_age"] == 62


def test_disclosure_inputs_upsert(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "act", "pw", "actuary", NOW)
    sid = store.create_submission(db, cid, uid, "census.xlsx", "/tmp/x.xlsx",
                                  "2025-12-31", "submitted", 100, 0, 0, NOW)
    assert store.get_disclosure_inputs(db, sid) is None

    store.save_disclosure_inputs(db, sid, {
        "dbo_begin": 1.8e9, "plan_assets": 1.2e9, "contributions": 1e8,
        "remeasure_financial": -5e6,
    }, uid, NOW)
    di = store.get_disclosure_inputs(db, sid)
    assert di["dbo_begin"] == pytest.approx(1.8e9)
    assert di["contributions"] == pytest.approx(1e8)
    assert di["npc_conversion"] is None      # 미입력 항목은 NULL

    # 제출건당 1건 upsert(중복 생성 아님)
    store.save_disclosure_inputs(db, sid, {"dbo_begin": 2.0e9}, uid, NOW)
    assert store.get_disclosure_inputs(db, sid)["dbo_begin"] == pytest.approx(2.0e9)


def test_sales_and_interactions(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "admin", "pw", "admin", NOW)

    # 영업정보가 없어도 list_sales에 회사가 나와야 함
    assert any(s["company_name"] == "가나전자" for s in store.list_sales(db))

    store.save_sales(db, cid, {
        "contact_name": "김담당", "contact_title": "과장", "contact_phone": "010-0000-0000",
        "contact_email": "kim@x.com", "contract_status": "계약", "approval_status": "승인",
        "special_requests": "긴급", "received_date": "2025-12-01",
    }, uid, NOW)
    sv = store.get_sales(db, cid)
    assert sv["contact_name"] == "김담당" and sv["contract_status"] == "계약"

    store.add_interaction(db, cid, "2025-12-02", "통화", "초기 상담", "이영업", uid, NOW)
    store.add_interaction(db, cid, "2025-12-05", "미팅", "계약 협의", "이영업", uid, NOW)
    inter = store.list_interactions(db, cid)
    assert len(inter) == 2
    assert inter[0]["itype"] in {"통화", "미팅"}


def test_prior_records_and_files(db):
    cid = auth.create_company(db, "가나전자", NOW)
    uid = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)

    store.save_prior_record(db, cid, {
        "prior_firm": "옛계리법인", "prior_valuation_date": "2024-12-31",
        "prior_dbo": 1.0e10, "prior_discount_rate": 4.0, "prior_salary_rate": 3.0,
        "prior_notes": "이관",
    }, uid, NOW)
    pr = store.get_prior_record(db, cid)
    assert pr["prior_firm"] == "옛계리법인"
    assert pr["prior_dbo"] == pytest.approx(1.0e10)

    store.add_prior_file(db, cid, "전기보고서.pdf", "/p/a.pdf", uid, NOW)
    store.add_prior_file(db, cid, "규약.jpg", "/p/b.jpg", uid, NOW)
    files = store.list_prior_files(db, cid)
    assert {f["filename"] for f in files} == {"전기보고서.pdf", "규약.jpg"}


def test_list_submissions_isolated_by_company(db):
    c1 = auth.create_company(db, "가나", NOW)
    c2 = auth.create_company(db, "다라", NOW)
    u1 = auth.create_user(db, "u1", "pw", "client", NOW, company_id=c1)
    u2 = auth.create_user(db, "u2", "pw", "client", NOW, company_id=c2)
    store.create_submission(db, c1, u1, "a.xlsx", "/p/a", "2025-12-31", "submitted", 10, 0, 0, NOW)
    store.create_submission(db, c2, u2, "b.xlsx", "/p/b", "2025-12-31", "submitted", 20, 0, 0, NOW)

    # 회사1 담당자는 회사1 것만 봐야 함
    assert {s["company_name"] for s in store.list_submissions(db, company_id=c1)} == {"가나"}
    assert {s["company_name"] for s in store.list_submissions(db, company_id=c2)} == {"다라"}


def test_qa_messages_thread(db):
    cid = auth.create_company(db, "가나전자", NOW)
    cu = auth.create_user(db, "clientA", "pw", "client", NOW, company_id=cid)
    au = auth.create_user(db, "act", "pw", "actuary", NOW)
    store.add_qa_message(db, cid, None, cu, "client", "할인율 근거가 궁금합니다.", NOW)
    store.add_qa_message(db, cid, None, au, "actuary", "우량회사채 수익률 기준입니다.", NOW)
    msgs = store.list_qa_messages(db, cid)
    assert len(msgs) == 2
    assert msgs[0]["author_role"] == "client" and msgs[1]["author_role"] == "actuary"
    # 마지막이 계리인 답변 → 미답변 목록에 없음
    assert cid not in store.unanswered_companies(db)
    # 기업이 다시 질문 → 미답변
    store.add_qa_message(db, cid, None, cu, "client", "추가 질문 있습니다.", "2025-12-31T01:00:00")
    assert cid in store.unanswered_companies(db)


def test_reupload_same_valdate_keeps_submitted_no_dup(db):
    """같은 산출기준일 재업로드는 '삭제+신규'가 아니라 제자리 갱신이어야 한다.

    회귀: 신청(submitted)된 건을 재업로드하면 삭제되고 validated 새 건이 생겨
    계리사 목록에서 사라지던 버그. 재업로드해도 (1) 중복 생성 없음,
    (2) 신청 상태 유지(계리사 조회 유지), (3) 명부는 교체되어야 한다.
    """
    cid = auth.create_company(db, "리업로드사", NOW)
    uid = auth.create_user(db, "clientRU", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c1.xlsx", "/p1", "2025-12-31",
                                  "validated", 10, 0, 0, NOW)
    store.update_submission_status(db, sid, "submitted", NOW)

    same = [s for s in store.list_submissions(db, company_id=cid)
            if s["valuation_date"] == "2025-12-31"]
    editable = [s for s in same if s["status"] in store.CLIENT_EDITABLE]
    assert editable and editable[0]["id"] == sid
    target = editable[0]
    was_submitted = target["status"] == "submitted"
    store.update_submission_file(db, target["id"], "c2.xlsx", "/p2", 11, 0, 0, NOW)
    if not was_submitted:
        store.update_submission_status(db, target["id"], "validated", NOW)

    subs = store.list_submissions(db, company_id=cid)
    assert len(subs) == 1                     # 중복 생성 없음
    assert subs[0]["id"] == sid               # 식별자 유지
    assert subs[0]["status"] == "submitted"   # 신청 상태 유지 → 계리사 조회 유지
    assert subs[0]["n_records"] == 11         # 명부는 교체됨


def test_reupload_before_submit_updates_in_place(db):
    """신청 전(validated) 재업로드도 제자리 갱신 — 중복 신청건이 생기지 않는다."""
    cid = auth.create_company(db, "리업로드사2", NOW)
    uid = auth.create_user(db, "clientRU2", "pw", "client", NOW, company_id=cid)
    sid = store.create_submission(db, cid, uid, "c1.xlsx", "/p1", "2025-12-31",
                                  "validated", 10, 2, 0, NOW)
    same = [s for s in store.list_submissions(db, company_id=cid)
            if s["valuation_date"] == "2025-12-31"]
    editable = [s for s in same if s["status"] in store.CLIENT_EDITABLE]
    target = editable[0]
    store.update_submission_file(db, target["id"], "c2.xlsx", "/p2", 10, 0, 0, NOW)
    store.update_submission_status(db, target["id"], "validated", NOW)
    subs = store.list_submissions(db, company_id=cid)
    assert len(subs) == 1 and subs[0]["n_errors"] == 0
