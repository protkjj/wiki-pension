"""engine.py 단계별 손계산 검증 테스트.

극단 단순 케이스에서 시작해 할인율 → 임금상승률 → 퇴직률을 하나씩 추가하며,
각 기대값의 손계산 과정을 주석으로 남긴다.

기본 convention (config 기본값):
  decrement_timing=end_of_year, salary_increase_timing=start_of_year,
  discount_timing=end_of_year, service_day_count=act/365, retirement_rate_basis=age
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from dbo.config import Config
from dbo.decrement import DecrementTables
from dbo.engine import calculate_census, calculate_employee
from dbo.models import Employee, PlanType

VAL_DATE = date(2025, 12, 31)


# ---------------------------------------------------------------------------
# 헬퍼: 설정·탈퇴율 테이블·종업원 구성
# ---------------------------------------------------------------------------


def _config(discount=0.0, salary_increase=0.0, **over) -> Config:
    base = dict(
        valuation_date="2025-12-31",
        discount_rate=discount,
        salary_increase_rate=salary_increase,
        retirement_age=60,
    )
    base.update(over)
    return Config.from_dict(base)


def _tables(ret_by_age=None, mortality=0.0, ages=range(15, 101)) -> DecrementTables:
    """연령별 퇴직률(dict) + 상수 사망률로 테이블 구성. 미지정 연령 퇴직률=0."""
    ret_by_age = ret_by_age or {}
    ages = list(ages)
    ret_df = pd.DataFrame({"age": ages, "rate": [ret_by_age.get(a, 0.0) for a in ages]})
    mort_df = pd.DataFrame(
        {"age": ages, "male_qx": [mortality] * len(ages), "female_qx": [mortality] * len(ages)}
    )
    return DecrementTables(retirement_by_age=ret_df, mortality=mort_df)


def _emp(birth: date, hire: date, salary=3_000_000, **over) -> Employee:
    base = dict(
        emp_id="T1",
        birth_date=birth,
        gender="M",
        hire_date=hire,
        base_salary=salary,
        current_year_accrual=0,
        emp_class="REGULAR",
    )
    base.update(over)
    return Employee(**base)


# 도달연령이 정확히 59세 → 정년(60)까지 N=1. 도달근속 s0 노출값 사용.
BIRTH_59 = date(1966, 12, 31)   # 2025-12-31 기준 만 59세
BIRTH_58 = date(1967, 12, 31)   # 만 58세 → N=2
HIRE_20Y = date(2005, 12, 31)   # 근속 약 20년


# ---------------------------------------------------------------------------
# 1) 극단 단순: 퇴직률0·사망률0·정년까지1년·할인0·임금상승0
#    → DBO = 기준급여 × 근속(총) × 재직비율 = base × S(1) × (s0/S(1)) = base × s0
# ---------------------------------------------------------------------------


def test_baseline_dbo_equals_salary_times_service():
    emp = _emp(BIRTH_59, HIRE_20Y)
    cfg = _config(discount=0.0, salary_increase=0.0)
    tables = _tables(mortality=0.0)

    res = calculate_employee(emp, cfg, tables)

    # N=1: 유일한 탈퇴는 t=1 정년, 확률 1.
    assert res.n_years == 1
    s0 = res.attained_service
    # 손계산: sal(1)=base(임금상승0), S(1)=s0+1, 재직비율=s0/(s0+1), disc=1
    #   DBO = base×(s0+1) × s0/(s0+1) × 1 × 1 = base×s0
    expected = emp.base_salary * s0
    assert res.dbo == pytest.approx(expected, abs=1.0)
    # CSC(one_year_slice) = sal(1)×1×1×1 = base
    assert res.csc == pytest.approx(emp.base_salary, abs=1.0)


# ---------------------------------------------------------------------------
# 2) 할인율만 추가: i>0, N=1 → 탈퇴시점 t=1, disc=1/(1+i)
#    → DBO = base × s0 × v
# ---------------------------------------------------------------------------


def test_add_discount_only():
    emp = _emp(BIRTH_59, HIRE_20Y)
    i = 0.05
    cfg = _config(discount=i, salary_increase=0.0)
    tables = _tables(mortality=0.0)

    res = calculate_employee(emp, cfg, tables)
    s0 = res.attained_service
    v = 1.0 / (1.0 + i)
    # 손계산: DBO = base×(s0+1)×[s0/(s0+1)]×1×v = base×s0×v
    expected = emp.base_salary * s0 * v
    assert res.dbo == pytest.approx(expected, abs=1.0)


# ---------------------------------------------------------------------------
# 3) 임금상승률만 추가 (N=2로 상승 효과가 드러나게): g>0, 할인0, 탈퇴0
#    salary_increase_timing=start_of_year → 정년(t=2) 급여지수 = t-1 = 1
#    → sal(2)=base×(1+g),  단일 정년탈퇴 확률 1
#    → DBO = base×(1+g) × s0
# ---------------------------------------------------------------------------


def test_add_salary_increase_only():
    emp = _emp(BIRTH_58, HIRE_20Y)      # N=2
    g = 0.03
    cfg = _config(discount=0.0, salary_increase=g)
    tables = _tables(mortality=0.0)

    res = calculate_employee(emp, cfg, tables)
    assert res.n_years == 2
    s0 = res.attained_service
    # 손계산: 탈퇴0이므로 t=1 탈퇴없음, t=2 정년 확률1.
    #   sal(2)=base×(1+g)^(2-1)=base×(1+g), S(2)=s0+2, 재직비율=s0/(s0+2), disc=1
    #   DBO = base×(1+g)×(s0+2) × s0/(s0+2) = base×(1+g)×s0
    expected = emp.base_salary * (1.0 + g) * s0
    assert res.dbo == pytest.approx(expected, abs=1.0)


# ---------------------------------------------------------------------------
# 4) 퇴직률만 추가 (효과가 드러나게 할인 포함): N=2, 1년차 퇴직률 q, 할인 i, 임금상승0, 사망0
#    t=1: 퇴직 확률 q, 탈퇴시점 t=1, disc=v,   기여 = base×s0×q×v
#    t=2: 정년 확률 (1-q), 탈퇴시점 t=2, disc=v^2, 기여 = base×s0×(1-q)×v^2
#    → DBO = base×s0×[ q·v + (1-q)·v² ]
# ---------------------------------------------------------------------------


def test_add_retirement_rate_only():
    emp = _emp(BIRTH_58, HIRE_20Y)      # N=2, 1년차 도달연령=58
    q = 0.10
    i = 0.05
    cfg = _config(discount=i, salary_increase=0.0)
    tables = _tables(ret_by_age={58: q}, mortality=0.0)   # 58세 퇴직률 q

    res = calculate_employee(emp, cfg, tables)
    assert res.n_years == 2
    s0 = res.attained_service
    v = 1.0 / (1.0 + i)
    # 손계산 (위 주석):
    expected = emp.base_salary * s0 * (q * v + (1.0 - q) * v ** 2)
    assert res.dbo == pytest.approx(expected, abs=1.0)

    # 상세 테이블 확률 검증
    d = res.detail
    assert d.loc[0, "당기퇴직확률"] == pytest.approx(q, rel=1e-9)
    assert d.loc[1, "정년도달확률"] == pytest.approx(1.0 - q, rel=1e-9)
    # 모든 탈퇴확률 합 = 1
    total_exit = (d["당기퇴직확률"] + d["당기사망확률"] + d["정년도달확률"]).sum()
    assert total_exit == pytest.approx(1.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 5) 사망률만 추가: N=2, 1년차 사망률 qd, 할인 i, 임금상승0, 퇴직0
#    t=1: 사망 확률 qd, disc=v,      기여 = base×s0×qd×v
#    t=2: 정년 확률 (1-qd), disc=v², 기여 = base×s0×(1-qd)×v²
#    → DBO = base×s0×[ qd·v + (1-qd)·v² ]  (사망도 적립급여 지급 가정)
# ---------------------------------------------------------------------------


def test_add_mortality_only():
    emp = _emp(BIRTH_58, HIRE_20Y)
    qd = 0.02
    i = 0.05
    cfg = _config(discount=i, salary_increase=0.0)
    tables = _tables(mortality=qd)

    res = calculate_employee(emp, cfg, tables)
    s0 = res.attained_service
    v = 1.0 / (1.0 + i)
    expected = emp.base_salary * s0 * (qd * v + (1.0 - qd) * v ** 2)
    assert res.dbo == pytest.approx(expected, abs=1.0)


# ---------------------------------------------------------------------------
# 제도구분 처리
# ---------------------------------------------------------------------------


def test_plan_type_2_uses_accrual_directly():
    # 간편법: 당년도추계액을 그대로 부채로.
    emp = _emp(BIRTH_58, HIRE_20Y, plan_type=2, current_year_accrual=12_345_678)
    cfg = _config()
    res = calculate_employee(emp, cfg, _tables())
    assert res.dbo == 12_345_678
    assert res.csc == 0.0
    assert res.detail is None


def test_plan_type_3_excluded_from_census():
    emps = [
        _emp(BIRTH_58, HIRE_20Y, emp_id="A", plan_type=1),
        _emp(BIRTH_58, HIRE_20Y, emp_id="B", plan_type=3),   # 제외
    ]
    out = calculate_census(emps, _config(), _tables())
    ids = {r.emp_id for r in out.results}
    assert ids == {"A"}
    assert out.excluded_emp_ids == ["B"]


def test_census_totals_and_subtotals():
    emps = [
        _emp(BIRTH_59, HIRE_20Y, emp_id="R1", emp_class="REGULAR", plan_type=1),
        _emp(BIRTH_59, HIRE_20Y, emp_id="E1", emp_class="EXECUTIVE", plan_type=1),
        _emp(BIRTH_58, HIRE_20Y, emp_id="S1", emp_class="CONTRACT", plan_type=2, current_year_accrual=1_000_000),
    ]
    cfg = _config()
    out = calculate_census(emps, cfg, _tables())
    # 총액 = 개인 합
    assert out.total_dbo == pytest.approx(sum(r.dbo for r in out.results))
    # 구분별 소계 인원
    assert out.subtotal_by_class["REGULAR"]["count"] == 1
    assert out.subtotal_by_class["EXECUTIVE"]["count"] == 1
    assert out.subtotal_by_plan[2]["DBO"] == 1_000_000


# ---------------------------------------------------------------------------
# 중간정산자: 근속 기산이 중간정산기준일부터
# ---------------------------------------------------------------------------


def test_interim_settlement_resets_service():
    interim = date(2020, 12, 31)   # 근속 약 5년으로 리셋
    emp = _emp(
        BIRTH_59, HIRE_20Y,
        interim_settlement_date=interim,
        interim_settlement_amount=10_000_000,
    )
    cfg = _config(discount=0.0, salary_increase=0.0)
    res = calculate_employee(emp, cfg, _tables())
    # s0 ≈ 5년 (중간정산기준일부터), DBO = base×s0
    assert res.attained_service == pytest.approx(5.0, abs=0.05)
    assert res.dbo == pytest.approx(emp.base_salary * res.attained_service, abs=1.0)
