"""census.py 테스트: 검증 규칙별 오류/경고 분류, DataFrame 로딩."""

from datetime import date

import pandas as pd

from dbo.census import (
    Severity,
    records_from_dataframe,
    validate_census,
)
from dbo.models import Employee

VAL_DATE = date(2025, 12, 31)


def _emp(**over) -> Employee:
    base = dict(
        emp_id="10001",
        birth_date=date(1985, 5, 1),
        gender="M",
        hire_date=date(2010, 3, 1),
        base_salary=3_000_000,
        current_year_accrual=50_000_000,
        emp_class="REGULAR",
    )
    base.update(over)
    return Employee(**base)


def _rules(report, severity=None):
    return {
        i.rule for i in report.issues if severity is None or i.severity == severity
    }


# -- 정상 케이스 -------------------------------------------------------------


def test_clean_record_has_no_issues():
    report = validate_census([_emp()], VAL_DATE)
    assert not report.has_errors
    assert len(report.warnings) == 0


# -- 오류(계산 중단) 규칙 ----------------------------------------------------


def test_hire_after_valuation_is_error():
    report = validate_census([_emp(hire_date=date(2026, 6, 1))], VAL_DATE)
    assert "hire_after_valuation" in _rules(report, Severity.ERROR)
    assert report.has_errors


def test_age_below_min_is_error():
    # 기준일 만 나이 < 15 → 오류. 2015년생이면 기준일(2025-12-31)에 만 10세.
    report = validate_census(
        [_emp(birth_date=date(2015, 1, 1), hire_date=date(2025, 6, 1))], VAL_DATE
    )
    assert "age_below_min" in _rules(report, Severity.ERROR)


def test_interim_before_hire_is_error():
    report = validate_census(
        [
            _emp(
                interim_settlement_date=date(2009, 1, 1),  # 입사(2010-03-01) 이전
                interim_settlement_amount=10_000_000,
            )
        ],
        VAL_DATE,
    )
    assert "interim_before_hire" in _rules(report, Severity.ERROR)


def test_base_salary_nonpositive_is_error():
    report = validate_census([_emp(base_salary=0)], VAL_DATE)
    assert "base_salary_nonpositive" in _rules(report, Severity.ERROR)


def test_birth_after_hire_is_error():
    report = validate_census(
        [_emp(birth_date=date(2011, 1, 1), hire_date=date(2010, 3, 1))], VAL_DATE
    )
    assert "birth_after_hire" in _rules(report, Severity.ERROR)


# -- 경고(플래그 후 진행) 규칙 -----------------------------------------------


def test_age_above_max_is_warning():
    # 기준일 만 나이 > 80 → 경고. 1940년생이면 기준일에 만 85세.
    report = validate_census([_emp(birth_date=date(1940, 1, 1))], VAL_DATE)
    assert "age_above_max" in _rules(report, Severity.WARNING)
    assert not report.has_errors  # 경고뿐이므로 계산은 진행 가능


def test_interim_amount_missing_is_warning():
    report = validate_census(
        [_emp(interim_settlement_date=date(2018, 1, 1))], VAL_DATE
    )
    assert "interim_amount_missing" in _rules(report, Severity.WARNING)
    assert not report.has_errors


def test_negative_accrual_is_warning():
    report = validate_census([_emp(current_year_accrual=-100)], VAL_DATE)
    assert "accrual_negative" in _rules(report, Severity.WARNING)


def test_flagged_emp_ids_collects_warning_subjects():
    report = validate_census(
        [_emp(emp_id="W1", current_year_accrual=-1), _emp(emp_id="OK")], VAL_DATE
    )
    assert report.flagged_emp_ids() == {"W1"}


# -- DataFrame 로딩 ----------------------------------------------------------


def test_records_from_dataframe_missing_column_is_error():
    df = pd.DataFrame([{"emp_id": "1", "gender": "M"}])  # 대부분 필수 컬럼 누락
    records, report = records_from_dataframe(df)
    assert records == []
    assert "missing_columns" in _rules(report, Severity.ERROR)


def test_sensitive_columns_blocked_with_guidance():
    # 실명·주민번호·연락처 컬럼은 오류로 차단, 부서 등은 경고 후 무시하고 파싱은 진행.
    df = pd.DataFrame([
        {
            "emp_id": "1", "birth_date": "1985-05-01", "gender": "M",
            "hire_date": "2010-03-01", "base_salary": 3_000_000,
            "current_year_accrual": 5_000_000, "emp_class": "REGULAR",
            "성명": "홍길동", "주민등록번호": "850501-1234567",
            "연락처": "010-1234-5678", "부서": "영업팀",
        }
    ])
    records, report = records_from_dataframe(df)
    assert len(records) == 1                              # 민감/불필요 컬럼 제거 후 파싱 성공
    rules = {i.rule for i in report.errors}
    assert "sensitive_column" in rules
    # 성명·주민번호·연락처 3건 차단
    assert sum(1 for i in report.errors if i.rule == "sensitive_column") == 3
    assert report.has_errors
    # 부서는 무시 경고
    assert any(i.rule == "ignored_columns" for i in report.warnings)


def test_gender_column_not_flagged_as_name():
    # '성별'이 '성명'으로 오탐되면 안 됨 (표준 컬럼 화이트리스트).
    from dbo.census import detect_sensitive_columns
    cols = ["emp_id", "gender", "성별", "base_salary"]
    assert detect_sensitive_columns(cols) == []


def test_norm_header_strips_annotations():
    # 줄바꿈·괄호 주석(중첩 포함)이 붙은 헤더도 이름만 추출.
    from dbo.census import _norm_header
    assert _norm_header("성별\n(1:남자, 2:여자)") == "성별"
    assert _norm_header("당년도\n퇴직금추계액") == "당년도퇴직금추계액"
    assert _norm_header("직종구분\n(1:직원, 3:임원, \n6:임원(PUC), 7:계약직(PUC))") == "직종구분"
    assert _norm_header("사원번호(회사 임의번호)") == "사원번호"


def test_apply_column_map_annotated_headers_and_numeric_codes():
    # 실제 고객 양식: 줄바꿈·괄호 헤더 + 숫자코드(성별 1/2, 직종 1/3/4/6/7).
    from dbo.census import apply_column_map
    cmap = {
        "columns": {
            "emp_id": ["사원번호"], "birth_date": ["생년월일"], "gender": ["성별"],
            "hire_date": ["입사일자"], "base_salary": ["기준급여"],
            "current_year_accrual": ["당년도퇴직금추계액"],
            "next_year_accrual": ["차년도퇴직금추계액"],
            "emp_class": ["직종구분"],
        },
        "values": {
            "gender": {"1": "M", "2": "F"},
            "emp_class": {"1": "REGULAR", "3": "EXECUTIVE", "4": "CONTRACT",
                          "6": "EXECUTIVE", "7": "CONTRACT"},
        },
    }
    df = pd.DataFrame([
        {"사원번호": "ih001", "생년월일": "19850501",
         "성별\n(1:남자, 2:여자)": 1, "입사일자": "20100301",
         "기준급여": 3_000_000, "당년도\n퇴직금추계액": 5_000_000,
         "차년도\n퇴직금추계액": None,
         "직종구분\n(1:직원, 3:임원, \n6:임원(PUC), 7:계약직(PUC))": 3},
    ])
    mapped = apply_column_map(df, cmap)
    records, report = records_from_dataframe(mapped)
    assert not report.has_errors, [i.message for i in report.errors]
    assert len(records) == 1
    assert records[0].gender.value == "M"
    assert records[0].emp_class.value == "EXECUTIVE"


def test_records_from_dataframe_parses_valid_rows_and_flags_bad_ones():
    df = pd.DataFrame(
        [
            {
                "emp_id": "1",
                "birth_date": "1985-05-01",
                "gender": "M",
                "hire_date": "2010-03-01",
                "base_salary": 3_000_000,
                "current_year_accrual": 50_000_000,
                "emp_class": "REGULAR",
                "interim_settlement_date": None,  # nullable → NaN 정규화 확인
                "interim_settlement_amount": None,
            },
            {
                "emp_id": "2",
                "birth_date": "1990-01-01",
                "gender": "Z",  # 잘못된 성별 → 파싱 오류
                "hire_date": "2015-01-01",
                "base_salary": 2_500_000,
                "current_year_accrual": 30_000_000,
                "emp_class": "REGULAR",
                "interim_settlement_date": None,
                "interim_settlement_amount": None,
            },
        ]
    )
    records, report = records_from_dataframe(df)
    assert len(records) == 1                      # 유효행 1건만 통과
    assert records[0].emp_id == "1"
    assert any(i.rule.startswith("parse:") for i in report.errors)
    # 오류 메시지는 시스템 필드명(gender)이 아니라 사용자용 컬럼명(성별)으로 표시
    gender_err = next(i for i in report.errors if i.rule == "parse:gender")
    assert "성별" in gender_err.message
    assert "gender" not in gender_err.message


def test_plan_type_empty_defaults_to_db_normal():
    """제도구분 빈칸(구분 없음)은 오류가 아니라 기본 1(DB정상)으로 정상처리."""
    import numpy as np
    base = dict(
        birth_date="1985-05-01", gender="M", hire_date="2010-03-01",
        base_salary=3_000_000, current_year_accrual=50_000_000, emp_class="REGULAR",
    )
    df = pd.DataFrame([
        {"emp_id": "1", **base, "plan_type": None},     # 빈칸
        {"emp_id": "2", **base, "plan_type": np.nan},   # 엑셀 빈셀(NaN)
        {"emp_id": "3", **base, "plan_type": 2.0},      # float 2 → 간편법
        {"emp_id": "4", **base, "plan_type": "3"},      # 문자열 3 → 제외
    ])
    records, report = records_from_dataframe(df)
    assert len(records) == 4                       # 빈칸이라고 탈락하지 않음
    assert not any(i.rule == "parse:plan_type" for i in report.errors)
    got = {r.emp_id: int(r.plan_type) for r in records}
    assert got == {"1": 1, "2": 1, "3": 2, "4": 3}
