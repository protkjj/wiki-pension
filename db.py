"""SQLite 스키마·연결 (플랫폼 MVP).

경량 저장소로 stdlib sqlite3만 사용(추가 의존성 없음). 프로덕션에서는
PostgreSQL 등으로 확장 가능한 구조.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    pw_salt TEXT NOT NULL,
    role TEXT NOT NULL,               -- client | actuary | admin
    company_id INTEGER,               -- client는 소속 회사, actuary/admin은 NULL
    display_name TEXT,
    created TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    uploaded_by INTEGER NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    valuation_date TEXT NOT NULL,
    status TEXT NOT NULL,             -- needs_fix | validated | submitted | calculated | reported
    n_records INTEGER DEFAULT 0,
    n_errors INTEGER DEFAULT 0,
    n_warnings INTEGER DEFAULT 0,
    note TEXT DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    calculated_by INTEGER NOT NULL,
    total_dbo REAL,
    total_csc REAL,
    n_calc INTEGER,
    n_excluded INTEGER,
    xlsx_path TEXT,
    run_log_path TEXT,
    created TEXT NOT NULL,
    FOREIGN KEY (submission_id) REFERENCES submissions(id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS qa_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    submission_id INTEGER,           -- 특정 제출 건 관련(선택)
    author_id INTEGER,
    author_role TEXT,                -- client | actuary | admin
    body TEXT NOT NULL,
    created TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS company_sales (
    company_id INTEGER PRIMARY KEY,
    contact_name TEXT,               -- 담당자 이름
    contact_title TEXT,              -- 직급
    contact_phone TEXT,              -- 연락처
    contact_email TEXT,              -- 이메일
    contract_status TEXT,            -- 신규접수 | 견적 | 계약 | 진행중 | 완료 | 보류
    approval_status TEXT,            -- 대기 | 검토중 | 승인 | 반려
    special_requests TEXT,           -- 특별 요구사항
    received_date TEXT,              -- 접수일
    updated_by INTEGER,
    updated TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    ts TEXT,                         -- 상담 일시
    itype TEXT,                      -- 통화 | 미팅 | 이메일 | 기타
    summary TEXT,                    -- 요약
    staff TEXT,                      -- 담당 직원
    created_by INTEGER,
    created TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS prior_records (
    company_id INTEGER PRIMARY KEY,
    prior_firm TEXT,                 -- 전 계리법인
    prior_valuation_date TEXT,       -- 전기 산출기준일
    prior_dbo REAL,                  -- 전기말 확정급여채무
    prior_discount_rate REAL,        -- 전기 할인율(%)
    prior_salary_rate REAL,          -- 전기 임금상승률(%)
    prior_notes TEXT,
    updated_by INTEGER,
    updated TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS prior_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_by INTEGER,
    created TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS plan_info (
    company_id INTEGER PRIMARY KEY,
    plan_type TEXT,                  -- 미설정 | DB | DC | DB+DC 병행
    established_date TEXT,           -- 제도 설정일
    benefit_rule TEXT,               -- 단수제 | 누진제 | 기타
    multiplier_rule TEXT,            -- 누진 배율/산식 설명
    interim_allowed INTEGER,         -- 중간정산 허용 0/1
    interim_cycle TEXT,              -- 정산 주기
    retirement_age INTEGER,          -- 정년
    salary_basis TEXT,               -- 평균임금 | 통상임금 | 기타
    external_funding TEXT,           -- 사외적립: 없음 | DB | DC | IRP | 혼합
    funding_institution TEXT,        -- 적립 기관
    funding_ratio REAL,              -- 사외적립 비율(%)
    notes TEXT,                      -- 특이사항
    updated_by INTEGER,
    updated TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS disclosure_inputs (
    submission_id INTEGER PRIMARY KEY,   -- 회사 재무자료(주석공시 조정내역용)
    dbo_begin REAL,                  -- 기초 확정급여채무
    plan_assets_begin REAL,          -- 기초 사외적립자산
    plan_assets REAL,                -- 기말 사외적립자산
    interest_income REAL,            -- 사외적립자산 이자수익
    contributions REAL,              -- 기여금 납부액
    benefits_paid REAL,              -- 사외적립자산에서 지급한 급여
    benefits_paid_dbo REAL,          -- 확정급여채무에서 차감한 급여지급액
    asset_return REAL,               -- 사외적립자산의 수익(순이자 제외)
    net_interest REAL,               -- 순확정급여부채(자산)의 순이자
    remeasure_demographic REAL,      -- 인구통계적가정 변동 재측정손익
    remeasure_financial REAL,        -- 재무적가정 변동 재측정손익
    remeasure_experience REAL,       -- 가정과 실제 차이 재측정손익
    npc_conversion REAL,             -- 국민연금전환금
    updated_by INTEGER,
    updated TEXT,
    FOREIGN KEY (submission_id) REFERENCES submissions(id)
);
CREATE TABLE IF NOT EXISTS aux_census (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 보조 명부(퇴직자·전기말·전출입·3년·기타장기)
    company_id INTEGER NOT NULL,
    census_type TEXT NOT NULL,              -- 명부 종류 라벨
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_by INTEGER,
    created TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS other_lt (
    company_id INTEGER PRIMARY KEY,         -- 기타장기종업원급여 지급규정
    rules_json TEXT,                        -- 근속상 규정 표(JSON)
    note TEXT,
    updated_by INTEGER,
    updated TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS funding_status (
    company_id INTEGER PRIMARY KEY,         -- 사외적립자산 현황(운영현황)
    valuation_date TEXT,
    data_json TEXT,                         -- 항목별 금액(JSON, 산출기준일 단일)
    updated_by INTEGER,
    updated TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);
CREATE TABLE IF NOT EXISTS base_rate_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 기초율 세트(버전) — 계리사 마스터 데이터
    name TEXT NOT NULL,                     -- 세트 명칭
    source TEXT,                            -- 출처: 개발원2312 | 경험률 | 전년도보고서
    base_year TEXT,                         -- 기준연도(예: 2025 / 2312 개발원)
    period_kind TEXT,                       -- 당기 | 전기
    retirement_age INTEGER,                 -- 정년(세트 메타)
    avg_raise REAL,                         -- 평균승급률
    data_json TEXT NOT NULL,                -- 정규화 기초율표(JSON): 연령별 퇴직·사망(남/여)·승급률
    note TEXT,
    created_by INTEGER,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discount_curves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- 할인율 커브(기준일별) — 계리사 마스터 데이터
    name TEXT NOT NULL,                     -- 커브 명칭(예: 2025-12-31 AA+)
    valuation_date TEXT,                    -- 기준일
    rating TEXT,                            -- 신용등급(AA+ 등)
    period_kind TEXT,                       -- 당기 | 전기
    curve_json TEXT NOT NULL,               -- 만기별 spot rate(JSON): [{maturity, rate}, ...]
    single_rate REAL,                       -- 산출된 단일할인율(듀레이션 반영)
    duration REAL,                          -- 가중평균만기
    note TEXT,
    created_by INTEGER,
    created TEXT NOT NULL
);
"""


def get_conn(db_path: Union[str, Path]) -> sqlite3.Connection:
    """연결을 반환한다(row_factory=Row, FK 활성화)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 기존 DB에 나중에 추가된 컬럼들(있으면 무시, 없으면 ALTER로 추가).
_ADDED_COLUMNS = {
    "submissions": [("purpose", "TEXT"), ("calculator", "TEXT"), ("applicant", "TEXT"),
                    ("quote_amount", "REAL"), ("promised_date", "TEXT"),
                    ("quote_sent", "TEXT"), ("contract_sent", "TEXT"),
                    ("collection_status", "TEXT"),
                    ("calculated_at", "TEXT"), ("reported_at", "TEXT"),
                    ("client_confirmed_at", "TEXT"), ("calc_version", "INTEGER"),
                    ("accepted_at", "TEXT"), ("review_requested_at", "TEXT"),
                    ("audit_comment", "TEXT")],
    "company_sales": [("contact_mobile", "TEXT"), ("settlement_month", "TEXT"),
                      ("address", "TEXT")],
    "results": [("metrics_json", "TEXT"), ("base_rate_set_id", "INTEGER"),
                ("discount_curve_id", "INTEGER")],
    "plan_info": [("detail_json", "TEXT")],   # 확장 제도항목(임금피크·지급률가감·임금인상율 등)
    "qa_messages": [("title", "TEXT"), ("parent_id", "INTEGER"), ("qno", "INTEGER")],
    # 기초율 세트: 경험기초율(회사별)·개발원(공용) 구분. kind='dev'|'experience'
    "base_rate_sets": [("company_id", "INTEGER"), ("kind", "TEXT")],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def init_db(db_path: Union[str, Path]) -> None:
    """스키마를 생성하고(존재하면 무시) 신규 컬럼을 보강한다."""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        conn.commit()
    finally:
        conn.close()
