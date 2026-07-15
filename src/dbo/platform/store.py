"""제출·결과·감사로그 저장소 (플랫폼 MVP)."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

from .db import get_conn

# 워크플로우 상태 (라벨)
STATUS_LABELS = {
    "needs_fix": "🔴 수정 필요",
    "validated": "🟡 검증완료(제출 전)",
    "submitted": "📤 신청됨",
    "accepted": "📥 접수완료",
    "calculated": "🧮 계산완료",
    "client_review": "🔎 기업검토요청",
    "on_hold": "⏸ 보류중",
    "cancelled": "🚫 취소",
    "reported": "✅ 보고완료",
}
STATUS_ORDER = ["needs_fix", "validated", "submitted", "accepted", "calculated",
                "client_review", "on_hold", "reported"]

# 완료(보고서 확정) 건만 삭제를 잠근다.
COMPLETED_STATUSES = {"reported"}
LOCKED_STATUSES = COMPLETED_STATUSES   # 하위호환 별칭
# 기업이 수정·삭제 가능한 단계 = 계리사 접수 전(신청 단계)
CLIENT_EDITABLE = {"needs_fix", "validated", "submitted"}
# 계리사가 수정 가능한 단계 = 보고완료 전 (기업검토요청 단계는 기업 확인 대기)
ACTUARY_EDITABLE = {"needs_fix", "validated", "submitted", "accepted", "calculated", "on_hold"}

# 계리사 화면의 기업고객 단계(고객 관점) — 내부 status를 단계로 묶는다.
STAGE_LABELS = ["신청", "접수완료", "계산완료", "기업검토요청", "보고완료"]
_STATUS_TO_STAGE = {
    "needs_fix": "신청",
    "validated": "신청",
    "submitted": "신청",
    "accepted": "접수완료",
    "calculated": "계산완료",
    "client_review": "기업검토요청",
    "on_hold": "보류중",
    "cancelled": "취소",
    "reported": "보고완료",
}
# 계리사 기업조회의 '접수여부' 표기
_STATUS_TO_ACCEPT = {
    "submitted": "신청", "accepted": "접수", "on_hold": "보류", "cancelled": "취소",
    "calculated": "접수", "reported": "접수",
    "needs_fix": "신청전", "validated": "신청전",
}


def stage_of(status: str) -> str:
    """내부 제출 상태를 고객 단계 라벨로 변환."""
    return _STATUS_TO_STAGE.get(status, status)


def acceptance_of(status: str) -> str:
    """계리사 기업조회의 접수여부(신청/접수/취소/보류) 표기."""
    return _STATUS_TO_ACCEPT.get(status, status)


def can_client_modify(status: str) -> bool:
    """기업이 수정·삭제 가능한지 — 계리사 접수 전(신청 단계)만."""
    return status in CLIENT_EDITABLE


def can_actuary_delete(status: str) -> bool:
    """계리사 삭제 가능 여부 — 보고완료 전이면 가능."""
    return status not in COMPLETED_STATUSES


def can_delete_submission(status: str) -> bool:
    """(하위호환) 기업 기준 삭제 가능 여부 = 접수 전."""
    return status in CLIENT_EDITABLE


def find_open_submission(db_path, company_id: int, valuation_date: str) -> Optional[dict]:
    """같은 회사·산출기준일의 '기업 수정가능(접수 전)' 신청건을 찾는다. 없으면 None."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE company_id=? AND valuation_date=? "
            "ORDER BY id DESC", (company_id, valuation_date)).fetchall()
        for r in rows:
            if r["status"] in CLIENT_EDITABLE:
                return _norm_sub(dict(r))
        return None
    finally:
        conn.close()


def delete_submission(db_path, submission_id: int) -> bool:
    """제출 건 삭제(결과·재무자료 포함). 완료(보고서 확정) 건이면 거부하고 False 반환."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT status FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if row is None or row["status"] in COMPLETED_STATUSES:
            return False
        conn.execute("DELETE FROM results WHERE submission_id=?", (submission_id,))
        conn.execute("DELETE FROM disclosure_inputs WHERE submission_id=?", (submission_id,))
        conn.execute("DELETE FROM submissions WHERE id=?", (submission_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def log_action(db_path, user_id: Optional[int], action: str, now: str, detail: str = "") -> None:
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO audit_log(ts, user_id, action, detail) VALUES(?,?,?,?)",
            (now, user_id, action, detail),
        )
        conn.commit()
    finally:
        conn.close()


def list_companies(db_path) -> List[dict]:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM companies ORDER BY name")]
    finally:
        conn.close()


def create_submission(
    db_path, company_id: int, uploaded_by: int, filename: str, stored_path: str,
    valuation_date: str, status: str, n_records: int, n_errors: int, n_warnings: int, now: str,
    purpose: str = None, calculator: str = None, applicant: str = None,
) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO submissions(company_id, uploaded_by, filename, stored_path, valuation_date,"
            " status, n_records, n_errors, n_warnings, purpose, calculator, applicant, created, updated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, uploaded_by, filename, stored_path, valuation_date, status,
             n_records, n_errors, n_warnings, purpose, calculator, applicant, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


COLLECTION_STATUSES = ["미청구", "계산서발급", "미수", "수금완료"]


def update_submission_file(db_path, submission_id: int, filename: str, stored_path: str,
                           n_records: int, n_errors: int, n_warnings: int, now: str) -> None:
    """계리사가 명부를 수정·재업로드하여 갱신(파일·검증 카운트 교체)."""
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE submissions SET filename=?, stored_path=?, n_records=?, n_errors=?, "
            "n_warnings=?, updated=? WHERE id=?",
            (filename, stored_path, n_records, n_errors, n_warnings, now, submission_id))
        conn.commit()
    finally:
        conn.close()


def set_submission_meta(db_path, submission_id: int, purpose: str = None,
                        calculator: str = None, quote_amount: float = None,
                        note: str = None, promised_date: str = None,
                        quote_sent: str = None, contract_sent: str = None,
                        collection_status: str = None) -> None:
    """산출목적·산출자·견적액·약속일·비고·영업(견적발송·계약서발송·수금상황) 갱신(주어진 것만)."""
    sets, params = [], []
    if purpose is not None:
        sets.append("purpose=?"); params.append(purpose)
    if calculator is not None:
        sets.append("calculator=?"); params.append(calculator)
    if quote_amount is not None:
        sets.append("quote_amount=?"); params.append(quote_amount)
    if note is not None:
        sets.append("note=?"); params.append(note)
    if promised_date is not None:
        sets.append("promised_date=?"); params.append(promised_date)
    if quote_sent is not None:
        sets.append("quote_sent=?"); params.append(quote_sent)
    if contract_sent is not None:
        sets.append("contract_sent=?"); params.append(contract_sent)
    if collection_status is not None:
        sets.append("collection_status=?"); params.append(collection_status)
    if not sets:
        return
    params.append(submission_id)
    conn = get_conn(db_path)
    try:
        conn.execute(f"UPDATE submissions SET {','.join(sets)} WHERE id=?", params)
        conn.commit()
    finally:
        conn.close()


def set_result_rate_refs(db_path, result_id: int, base_rate_set_id, discount_curve_id) -> None:
    """산출 결과에 사용한 기초율 세트·할인율 커브 ID를 기록(감사추적)."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE results SET base_rate_set_id=?, discount_curve_id=? WHERE id=?",
                     (base_rate_set_id, discount_curve_id, result_id))
        conn.commit()
    finally:
        conn.close()


def save_result_metrics(db_path, result_id: int, metrics: dict) -> None:
    """계산 결과의 부가지표(듀레이션·민감도·전기PBO 등)를 JSON으로 저장."""
    import json as _json
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE results SET metrics_json=? WHERE id=?",
                     (_json.dumps(metrics, ensure_ascii=False), result_id))
        conn.commit()
    finally:
        conn.close()


_STAGE_TIME_FIELDS = {"accepted_at", "review_requested_at", "client_confirmed_at",
                      "calculated_at", "reported_at"}


def stamp_stage_time(db_path, submission_id: int, field: str, now: str) -> None:
    """단계 전환 시각을 기록(허용된 컬럼만)."""
    if field not in _STAGE_TIME_FIELDS:
        raise ValueError(f"허용되지 않은 단계시각 컬럼: {field}")
    conn = get_conn(db_path)
    try:
        conn.execute(f"UPDATE submissions SET {field}=? WHERE id=?", (now, submission_id))
        conn.commit()
    finally:
        conn.close()


def mark_calculated(db_path, submission_id: int, now: str) -> int:
    """계산완료 처리 — calculated 상태 + 완료일시 + 버전 증가. 새 버전번호 반환.

    재계산은 이전 산출을 무효화하므로, 기업검토요청·검토완료 이력(review_requested_at,
    client_confirmed_at)을 초기화한다 → 기업이 바뀐 내용을 다시 확인하도록 재검토요청 필요.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT calc_version FROM submissions WHERE id=?",
                           (submission_id,)).fetchone()
        ver = (row["calc_version"] or 0) + 1 if row else 1
        conn.execute("UPDATE submissions SET status='calculated', calculated_at=?, "
                     "calc_version=?, client_confirmed_at=NULL, review_requested_at=NULL, "
                     "updated=? WHERE id=?", (now, ver, now, submission_id))
        conn.commit()
        return ver
    finally:
        conn.close()


def set_audit_comment(db_path, submission_id: int, comment: str, now: str) -> None:
    """감사 대응 Q&A에 대한 계리사 코멘트 저장(기업 화면에도 함께 표시)."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE submissions SET audit_comment=?, updated=? WHERE id=?",
                     (comment, now, submission_id))
        conn.commit()
    finally:
        conn.close()


def mark_client_confirmed(db_path, submission_id: int, now: str) -> None:
    """기업검토 확인 — 기업이 검토 확인한 시각 기록(상태는 client_review 유지)."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE submissions SET client_confirmed_at=?, updated=? WHERE id=?",
                     (now, now, submission_id))
        conn.commit()
    finally:
        conn.close()


def mark_reported(db_path, submission_id: int, now: str) -> None:
    """최종 보고완료 — reported 상태 + 보고완료일시 기록."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE submissions SET status='reported', reported_at=?, updated=? "
                     "WHERE id=?", (now, now, submission_id))
        conn.commit()
    finally:
        conn.close()


def update_submission_status(db_path, submission_id: int, status: str, now: str, note: str = None) -> None:
    conn = get_conn(db_path)
    try:
        if note is None:
            conn.execute("UPDATE submissions SET status=?, updated=? WHERE id=?",
                         (status, now, submission_id))
        else:
            conn.execute("UPDATE submissions SET status=?, updated=?, note=? WHERE id=?",
                         (status, now, note, submission_id))
        conn.commit()
    finally:
        conn.close()


# 나중에 추가된 선택 컬럼 — 마이그레이션이 안 된 DB에서도 KeyError가 나지 않도록 기본값 보장.
_SUBMISSION_OPTIONAL = ("purpose", "calculator", "applicant", "quote_amount", "note",
                        "promised_date", "quote_sent", "contract_sent", "collection_status",
                        "calculated_at", "reported_at", "client_confirmed_at", "calc_version",
                        "accepted_at", "review_requested_at", "audit_comment")


def _norm_sub(d: Optional[dict]) -> Optional[dict]:
    if d is None:
        return None
    for k in _SUBMISSION_OPTIONAL:
        d.setdefault(k, None)
    return d


def get_submission(db_path, submission_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT s.*, c.name AS company_name FROM submissions s "
            "JOIN companies c ON s.company_id=c.id WHERE s.id=?", (submission_id,)
        ).fetchone()
        return _norm_sub(dict(row)) if row else None
    finally:
        conn.close()


def list_submissions(
    db_path, company_id: Optional[int] = None, statuses: Optional[List[str]] = None
) -> List[dict]:
    q = ("SELECT s.*, c.name AS company_name FROM submissions s "
         "JOIN companies c ON s.company_id=c.id")
    conds, params = [], []
    if company_id is not None:
        conds.append("s.company_id=?"); params.append(company_id)
    if statuses:
        conds.append("s.status IN (%s)" % ",".join("?" * len(statuses))); params.extend(statuses)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY s.updated DESC"
    conn = get_conn(db_path)
    try:
        return [_norm_sub(dict(r)) for r in conn.execute(q, params)]
    finally:
        conn.close()


def save_result(
    db_path, submission_id: int, calculated_by: int, total_dbo: float, total_csc: float,
    n_calc: int, n_excluded: int, xlsx_path: str, run_log_path: str, now: str,
) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO results(submission_id, calculated_by, total_dbo, total_csc, n_calc,"
            " n_excluded, xlsx_path, run_log_path, created) VALUES(?,?,?,?,?,?,?,?,?)",
            (submission_id, calculated_by, total_dbo, total_csc, n_calc, n_excluded,
             xlsx_path, run_log_path, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def latest_result(db_path, submission_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM results WHERE submission_id=? ORDER BY created DESC LIMIT 1",
            (submission_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


PLAN_FIELDS = [
    "plan_type", "established_date", "benefit_rule", "multiplier_rule",
    "interim_allowed", "interim_cycle", "retirement_age", "salary_basis",
    "external_funding", "funding_institution", "funding_ratio", "notes",
]


def get_plan_info(db_path, company_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM plan_info WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


_PLAN_SAVE = PLAN_FIELDS + ["detail_json"]   # 확장 제도항목(JSON) 포함 저장


def save_plan_info(db_path, company_id: int, data: dict, user_id: int, now: str) -> None:
    """제도 정보 upsert (회사당 1건). data에 있는 키만 갱신하고 나머지는 기존값 유지."""
    existing = get_plan_info(db_path, company_id) or {}
    vals = {k: (data[k] if k in data else existing.get(k)) for k in _PLAN_SAVE}
    cols = ["company_id"] + _PLAN_SAVE + ["updated_by", "updated"]
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{k}=excluded.{k}" for k in _PLAN_SAVE + ["updated_by", "updated"])
    conn = get_conn(db_path)
    try:
        conn.execute(
            f"INSERT INTO plan_info({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(company_id) DO UPDATE SET {updates}",
            [company_id] + [vals[k] for k in _PLAN_SAVE] + [user_id, now],
        )
        conn.commit()
    finally:
        conn.close()


def get_funding_status(db_path, company_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM funding_status WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_funding_status(db_path, company_id: int, valuation_date: str, data_json: str,
                        user_id: int, now: str) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO funding_status(company_id, valuation_date, data_json, updated_by, updated) "
            "VALUES(?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET "
            "valuation_date=excluded.valuation_date, data_json=excluded.data_json, "
            "updated_by=excluded.updated_by, updated=excluded.updated",
            (company_id, valuation_date, data_json, user_id, now))
        conn.commit()
    finally:
        conn.close()


# 주석공시 조정내역용 회사 재무자료 (제출건당 1건) ------------------------------
DISCLOSURE_FIELDS = [
    "dbo_begin", "plan_assets_begin", "plan_assets", "interest_income",
    "contributions", "benefits_paid", "benefits_paid_dbo", "asset_return",
    "net_interest", "remeasure_demographic", "remeasure_financial",
    "remeasure_experience", "npc_conversion",
]


def get_disclosure_inputs(db_path, submission_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM disclosure_inputs WHERE submission_id=?", (submission_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_disclosure_inputs(db_path, submission_id: int, data: dict, user_id: int, now: str) -> None:
    """주석공시용 재무자료 upsert (제출건당 1건)."""
    vals = {k: data.get(k) for k in DISCLOSURE_FIELDS}
    cols = ["submission_id"] + DISCLOSURE_FIELDS + ["updated_by", "updated"]
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{k}=excluded.{k}" for k in DISCLOSURE_FIELDS + ["updated_by", "updated"])
    conn = get_conn(db_path)
    try:
        conn.execute(
            f"INSERT INTO disclosure_inputs({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(submission_id) DO UPDATE SET {updates}",
            [submission_id] + [vals[k] for k in DISCLOSURE_FIELDS] + [user_id, now],
        )
        conn.commit()
    finally:
        conn.close()


SALES_FIELDS = [
    "contact_name", "contact_title", "contact_phone", "contact_mobile", "contact_email",
    "settlement_month", "address",
    "contract_status", "approval_status", "special_requests", "received_date",
]
CONTRACT_STATUSES = ["신규접수", "견적", "계약", "진행중", "완료", "보류"]
APPROVAL_STATUSES = ["대기", "검토중", "승인", "반려"]
INTERACTION_TYPES = ["통화", "미팅", "이메일", "기타"]


def get_sales(db_path, company_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM company_sales WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_sales(db_path, company_id: int, data: dict, user_id: int, now: str) -> None:
    """영업/기업 기본정보 upsert. data에 있는 키만 갱신하고 나머지는 기존값 유지."""
    existing = get_sales(db_path, company_id) or {}
    merged = {k: (data[k] if k in data else existing.get(k)) for k in SALES_FIELDS}
    cols = ["company_id"] + SALES_FIELDS + ["updated_by", "updated"]
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{k}=excluded.{k}" for k in SALES_FIELDS + ["updated_by", "updated"])
    conn = get_conn(db_path)
    try:
        conn.execute(
            f"INSERT INTO company_sales({','.join(cols)}) VALUES({ph}) "
            f"ON CONFLICT(company_id) DO UPDATE SET {upd}",
            [company_id] + [merged[k] for k in SALES_FIELDS] + [user_id, now],
        )
        conn.commit()
    finally:
        conn.close()


def list_sales(db_path) -> List[dict]:
    """모든 회사(영업정보 없더라도 포함) 목록."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT c.id AS company_id, c.name AS company_name, s.contact_name, s.contact_title,"
            " s.contact_phone, s.contact_email, s.contract_status, s.approval_status,"
            " s.special_requests, s.received_date, s.updated "
            "FROM companies c LEFT JOIN company_sales s ON c.id=s.company_id ORDER BY c.name")
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_interaction(db_path, company_id: int, ts: str, itype: str, summary: str,
                    staff: str, user_id: int, now: str) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO interactions(company_id, ts, itype, summary, staff, created_by, created)"
            " VALUES(?,?,?,?,?,?,?)", (company_id, ts, itype, summary, staff, user_id, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_interactions(db_path, company_id: int) -> List[dict]:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM interactions WHERE company_id=? ORDER BY ts DESC, id DESC", (company_id,))]
    finally:
        conn.close()


PRIOR_FIELDS = [
    "prior_firm", "prior_valuation_date", "prior_dbo",
    "prior_discount_rate", "prior_salary_rate", "prior_notes",
]


def get_prior_record(db_path, company_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM prior_records WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_prior_record(db_path, company_id: int, data: dict, user_id: int, now: str) -> None:
    cols = ["company_id"] + PRIOR_FIELDS + ["updated_by", "updated"]
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{k}=excluded.{k}" for k in PRIOR_FIELDS + ["updated_by", "updated"])
    conn = get_conn(db_path)
    try:
        conn.execute(
            f"INSERT INTO prior_records({','.join(cols)}) VALUES({ph}) "
            f"ON CONFLICT(company_id) DO UPDATE SET {upd}",
            [company_id] + [data.get(k) for k in PRIOR_FIELDS] + [user_id, now],
        )
        conn.commit()
    finally:
        conn.close()


def add_prior_file(db_path, company_id: int, filename: str, stored_path: str,
                   user_id: int, now: str) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO prior_files(company_id, filename, stored_path, uploaded_by, created)"
            " VALUES(?,?,?,?,?)", (company_id, filename, stored_path, user_id, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_prior_files(db_path, company_id: int) -> List[dict]:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM prior_files WHERE company_id=? ORDER BY created DESC", (company_id,))]
    finally:
        conn.close()


# ── 보조 명부(퇴직자·전기말·전출입·3년·기타장기) ─────────────────────────────
def add_aux_census(db_path, company_id: int, census_type: str, filename: str,
                   stored_path: str, uploaded_by: int, now: str) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO aux_census(company_id, census_type, filename, stored_path, uploaded_by, created)"
            " VALUES(?,?,?,?,?,?)",
            (company_id, census_type, filename, stored_path, uploaded_by, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_aux_census(db_path, company_id: int, census_type: str = None) -> List[dict]:
    conn = get_conn(db_path)
    try:
        if census_type:
            rows = conn.execute(
                "SELECT * FROM aux_census WHERE company_id=? AND census_type=? ORDER BY created DESC",
                (company_id, census_type))
        else:
            rows = conn.execute(
                "SELECT * FROM aux_census WHERE company_id=? ORDER BY created DESC", (company_id,))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_aux_census(db_path, aux_id: int) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM aux_census WHERE id=?", (aux_id,))
        conn.commit()
    finally:
        conn.close()


# ── 기타장기종업원급여 지급규정 ──────────────────────────────────────────────
def get_other_lt(db_path, company_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM other_lt WHERE company_id=?", (company_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_other_lt(db_path, company_id: int, rules_json: str, note: str,
                  user_id: int, now: str) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO other_lt(company_id, rules_json, note, updated_by, updated) "
            "VALUES(?,?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET "
            "rules_json=excluded.rules_json, note=excluded.note, "
            "updated_by=excluded.updated_by, updated=excluded.updated",
            (company_id, rules_json, note, user_id, now))
        conn.commit()
    finally:
        conn.close()


def get_census_summary(db_path, company_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM census_summary WHERE company_id=?",
                           (company_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_census_summary(db_path, company_id: int, data_json: str,
                        user_id: int, now: str) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO census_summary(company_id, data_json, updated_by, updated) "
            "VALUES(?,?,?,?) ON CONFLICT(company_id) DO UPDATE SET "
            "data_json=excluded.data_json, updated_by=excluded.updated_by, "
            "updated=excluded.updated",
            (company_id, data_json, user_id, now))
        conn.commit()
    finally:
        conn.close()


def add_qa_message(db_path, company_id: int, submission_id, author_id: int,
                   author_role: str, body: str, now: str, title: str = None,
                   parent_id: int = None, qno: int = None) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO qa_messages(company_id, submission_id, author_id, author_role, body,"
            " title, parent_id, qno, created) VALUES(?,?,?,?,?,?,?,?,?)",
            (company_id, submission_id, author_id, author_role, body, title, parent_id, qno, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_question(db_path, company_id: int, author_id: int, title: str, body: str, now: str) -> int:
    """기업 질문 등록 — 회사별 질문번호(qno) 자동 부여."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(qno),0) AS m FROM qa_messages "
            "WHERE company_id=? AND parent_id IS NULL", (company_id,)).fetchone()
        qno = (row["m"] or 0) + 1
        cur = conn.execute(
            "INSERT INTO qa_messages(company_id, author_id, author_role, title, body, qno, created)"
            " VALUES(?,?,?,?,?,?,?)", (company_id, author_id, "client", title, body, qno, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_answer(db_path, company_id: int, author_id: int, parent_id: int, body: str, now: str) -> int:
    """계리인 답변 — 질문(parent_id)에 연결."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO qa_messages(company_id, author_id, author_role, body, parent_id, created)"
            " VALUES(?,?,?,?,?,?)", (company_id, author_id, "actuary", body, parent_id, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_qa_messages(db_path, company_id: int) -> List[dict]:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM qa_messages WHERE company_id=? ORDER BY created ASC, id ASC",
            (company_id,))]
    finally:
        conn.close()


def list_qa_threads(db_path, company_id: int) -> List[dict]:
    """질문(번호·제목·내용)마다 답변 목록을 중첩해 반환(최신 질문 먼저)."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT q.*, u.display_name AS author_name FROM qa_messages q "
            "LEFT JOIN users u ON q.author_id=u.id WHERE q.company_id=? ORDER BY q.id", (company_id,))]
    finally:
        conn.close()
    questions = [r for r in rows if r.get("parent_id") is None and r.get("author_role") == "client"]
    answers = [r for r in rows if r.get("parent_id") is not None]
    for q in questions:
        q["answers"] = [a for a in answers if a["parent_id"] == q["id"]]
    questions.sort(key=lambda q: -(q.get("qno") or q.get("id") or 0))
    return questions


def unanswered_companies(db_path) -> List[int]:
    """마지막 메시지가 기업(client)인 회사 = 계리인 답변 대기."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT company_id, author_role FROM qa_messages q1 WHERE id = "
            "(SELECT MAX(id) FROM qa_messages q2 WHERE q2.company_id = q1.company_id)")
        return [r["company_id"] for r in rows if r["author_role"] == "client"]
    finally:
        conn.close()


def qa_pending_counts(db_path) -> dict:
    """회사별 '계리인 답변 대기' 질문 수 = 답변(자식)이 없는 기업 질문 수."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT q.company_id AS cid, "
            "(SELECT COUNT(*) FROM qa_messages a WHERE a.parent_id=q.id) AS nans "
            "FROM qa_messages q WHERE q.parent_id IS NULL AND q.author_role='client'")]
    finally:
        conn.close()
    counts: dict = {}
    for r in rows:
        if not r["nans"]:
            counts[r["cid"]] = counts.get(r["cid"], 0) + 1
    return counts


def qa_question_rows(db_path) -> List[dict]:
    """관리자 질의응답현황용: 질문별 (날짜·번호·제목·내용·답변여부·답변자·기업명)."""
    conn = get_conn(db_path)
    try:
        qs = [dict(r) for r in conn.execute(
            "SELECT q.*, c.name AS company_name FROM qa_messages q "
            "JOIN companies c ON q.company_id=c.id "
            "WHERE q.parent_id IS NULL AND q.author_role='client' ORDER BY q.id")]
        ans = [dict(r) for r in conn.execute(
            "SELECT a.*, u.display_name AS author_name FROM qa_messages a "
            "LEFT JOIN users u ON a.author_id=u.id WHERE a.parent_id IS NOT NULL ORDER BY a.id")]
    finally:
        conn.close()
    out = []
    for q in qs:
        first = next((a for a in ans if a["parent_id"] == q["id"]), None)
        out.append({
            "date": (q["created"] or "")[:10], "qno": q.get("qno"), "title": q.get("title"),
            "body": q["body"], "company_name": q["company_name"],
            "answered": (first["created"][:10] if first else "-"),
            "answered_by": (first["author_name"] if first else "-"),
        })
    return sorted(out, key=lambda x: x["date"], reverse=True)


def status_counts(db_path) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM submissions GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 기초율 세트(버전) — 계리사 마스터 데이터
# ---------------------------------------------------------------------------

def add_base_rate_set(db_path, name: str, source: str, base_year: str, period_kind: str,
                      retirement_age, avg_raise, data_json: str, note: str,
                      user_id: Optional[int], now: str,
                      company_id: Optional[int] = None, kind: str = "dev") -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO base_rate_sets(name, source, base_year, period_kind, retirement_age, "
            "avg_raise, data_json, note, created_by, created, company_id, kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, source, base_year, period_kind, retirement_age, avg_raise,
             data_json, note, user_id, now, company_id, kind))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_base_rate_sets(db_path, kind: Optional[str] = None,
                        company_id: Optional[int] = None) -> List[dict]:
    """기초율 세트 목록. kind='dev'|'experience'로 구분, company_id로 회사 경험세트 필터.

    company_id 지정 시 '해당 회사 세트 + 공용(개발원) 세트'를 함께 반환한다.
    kind가 NULL인 기존 행은 'dev'로 간주.
    """
    conds, params = [], []
    if kind is not None:
        conds.append("COALESCE(kind,'dev')=?"); params.append(kind)
    if company_id is not None:
        conds.append("(company_id=? OR company_id IS NULL)"); params.append(company_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, source, base_year, period_kind, retirement_age, avg_raise, "
            "note, created, company_id, COALESCE(kind,'dev') AS kind "
            "FROM base_rate_sets" + where + " ORDER BY id DESC", params)
        return [dict(r) for r in rows]
    finally:
        conn.close()


def experience_upload_status(db_path) -> List[dict]:
    """회사별 '경험기초율 산출데이터' 업로드 여부 + 경험세트 존재 여부.

    반환: [{company_id, company_name, n_uploads, last_upload, n_sets}] — 업로드가 있는 회사만.
    계리사가 간과하지 않도록 '올렸는데 아직 세트 미산출'을 구분한다.
    """
    conn = get_conn(db_path)
    try:
        ups = [dict(r) for r in conn.execute(
            "SELECT a.company_id AS cid, c.name AS cname, COUNT(*) AS n, MAX(a.created) AS last "
            "FROM aux_census a JOIN companies c ON a.company_id=c.id "
            "WHERE a.census_type=? GROUP BY a.company_id",
            (EXPERIENCE_CENSUS_TYPE,))]
        sets = {r["company_id"]: r["n"] for r in conn.execute(
            "SELECT company_id, COUNT(*) AS n FROM base_rate_sets "
            "WHERE COALESCE(kind,'dev')='experience' GROUP BY company_id")}
    finally:
        conn.close()
    out = []
    for u in ups:
        out.append({"company_id": u["cid"], "company_name": u["cname"],
                    "n_uploads": u["n"], "last_upload": u["last"],
                    "n_sets": sets.get(u["cid"], 0)})
    return sorted(out, key=lambda x: (x["n_sets"] > 0, x["company_name"]))


EXPERIENCE_CENSUS_TYPE = "경험기초율산출데이터"


def get_base_rate_set(db_path, set_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM base_rate_sets WHERE id=?", (set_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def delete_base_rate_set(db_path, set_id: int) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM base_rate_sets WHERE id=?", (set_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 할인율 커브(기준일별) — 계리사 마스터 데이터
# ---------------------------------------------------------------------------

def add_discount_curve(db_path, name: str, valuation_date: str, rating: str, period_kind: str,
                       curve_json: str, single_rate, duration, note: str,
                       user_id: Optional[int], now: str) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO discount_curves(name, valuation_date, rating, period_kind, curve_json, "
            "single_rate, duration, note, created_by, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, valuation_date, rating, period_kind, curve_json, single_rate, duration,
             note, user_id, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_discount_curves(db_path) -> List[dict]:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, valuation_date, rating, period_kind, single_rate, duration, "
            "note, created FROM discount_curves ORDER BY id DESC")
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_discount_curve(db_path, curve_id: int) -> Optional[dict]:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM discount_curves WHERE id=?", (curve_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def delete_discount_curve(db_path, curve_id: int) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM discount_curves WHERE id=?", (curve_id,))
        conn.commit()
    finally:
        conn.close()


def discount_curve_in_use(db_path, curve_id: int) -> bool:
    """이 할인율 커브가 한 번이라도 산출결과에 사용되었는지."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT 1 FROM results WHERE discount_curve_id=? LIMIT 1",
                         (curve_id,)).fetchone()
        return r is not None
    finally:
        conn.close()


def base_rate_set_in_use(db_path, set_id: int) -> bool:
    """이 기초율 세트가 한 번이라도 산출결과에 사용되었는지."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT 1 FROM results WHERE base_rate_set_id=? LIMIT 1",
                         (set_id,)).fetchone()
        return r is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 데이터 초기화 (관리자) — 등록된 업무데이터 일괄 삭제
# ---------------------------------------------------------------------------

# 업무데이터 테이블(계정/기업 제외). 자식→부모 순서로 삭제.
_RESET_TABLES = [
    "results", "disclosure_inputs", "qa_messages", "aux_census", "other_lt",
    "funding_status", "plan_info", "prior_records", "prior_files",
    "interactions", "company_sales", "base_rate_sets", "discount_curves",
    "submissions", "audit_log",
]


def reset_platform_data(db_path, wipe_companies: bool = False) -> dict:
    """등록된 업무데이터를 모두 삭제한다. 반환: {테이블: 삭제건수}.

    기본은 계정(users)·기업(companies)은 남기고 신청·산출·제도·기초율 등만 삭제.
    wipe_companies=True면 기업고객 계정과 기업도 삭제(계리사·관리자 계정은 유지).
    """
    conn = get_conn(db_path)
    removed: dict = {}
    try:
        for t in _RESET_TABLES:
            cur = conn.execute(f"DELETE FROM {t}")
            removed[t] = cur.rowcount
        if wipe_companies:
            removed["users(client)"] = conn.execute(
                "DELETE FROM users WHERE role='client'").rowcount
            removed["companies"] = conn.execute("DELETE FROM companies").rowcount
        conn.commit()
    finally:
        conn.close()
    return removed
