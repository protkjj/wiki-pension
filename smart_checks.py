"""스마트 명부 검증 (로컬, 개인정보 외부 유출 없음).

기본 도메인 검증(census.validate_census)에 더해, 통계·논리적 이상을 자동으로
찾아낸다. 외부 AI/API를 호출하지 않으므로 명부(개인정보)가 기기를 벗어나지 않는다.

검출 항목:
  - 중복 사번(오류)
  - 입사 시 나이 15세 미만(경고, 날짜 오류 의심)
  - 종업원구분별 급여 이상치(로버스트 z-score, 경고)
  - 급여 절대 이상값(월 5천만 초과 / 100만 미만, 경고)
  - 당년도추계액과 예상(급여×근속) 괴리(경고)

임계값은 상단 상수로 조정 가능.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

import numpy as np

from .census import Severity, ValidationReport
from .models import Employee

# 조정 가능한 임계값
SALARY_ROBUST_Z = 5.0            # 구분별 급여 로버스트 z-score 경계
SALARY_ABS_HIGH = 50_000_000     # 월 기준급여 상한(초과 시 경고: 연봉 오입력 의심)
SALARY_ABS_LOW = 1_000_000       # 월 기준급여 하한(미만 시 경고: 단위/최저임금 확인)
ACCRUAL_RATIO_HIGH = 3.0         # 추계액/예상 상한
ACCRUAL_RATIO_LOW = 0.3          # 추계액/예상 하한
MIN_GROUP_FOR_OUTLIER = 5        # 이상치 판정 최소 표본


def _years(a: date, b: date) -> float:
    return (b - a).days / 365.25


def _class_of(emp: Employee) -> str:
    return emp.emp_class.value if hasattr(emp.emp_class, "value") else str(emp.emp_class)


def run_smart_checks(
    records: List[Employee],
    valuation_date: date,
    report: Optional[ValidationReport] = None,
) -> ValidationReport:
    """스마트 검증을 수행해 report에 이슈를 추가한다."""
    if report is None:
        report = ValidationReport(n_records=len(records))

    # 1) 중복 사번 (오류)
    seen: dict = {}
    for e in records:
        seen.setdefault(e.emp_id, 0)
        seen[e.emp_id] += 1
    for eid, cnt in seen.items():
        if cnt > 1:
            report.add(
                rule="duplicate_emp_id", severity=Severity.ERROR,
                message=f"사번이 {cnt}번 중복됩니다 — 사번은 고유해야 합니다.", emp_id=eid,
            )

    # (입사 연령 범위 검증은 계리사 표준 규칙 actuary_checks.hire_age_range에서 처리)

    # 3) 종업원구분별 급여 이상치 (로버스트 z-score)
    by_class: dict = {}
    for e in records:
        by_class.setdefault(_class_of(e), []).append(e)
    for cls, group in by_class.items():
        if len(group) < MIN_GROUP_FOR_OUTLIER:
            continue
        sals = np.array([e.base_salary for e in group], dtype=float)
        med = float(np.median(sals))
        mad = float(np.median(np.abs(sals - med))) or 1.0
        for e in group:
            z = 0.6745 * (e.base_salary - med) / mad
            if abs(z) > SALARY_ROBUST_Z:
                report.add(
                    rule="salary_outlier", severity=Severity.WARNING,
                    message=(f"기준급여 {e.base_salary:,.0f}원이 '{cls}' 중앙값 "
                             f"{med:,.0f}원 대비 크게 벗어남 — 확인하세요."),
                    emp_id=e.emp_id,
                )

    # 4) 급여 절대 이상값(상한). 하한은 계리사 표준 규칙(actuary_checks.salary_below_min)에서 처리.
    for e in records:
        if e.base_salary > SALARY_ABS_HIGH:
            report.add(
                rule="salary_too_high", severity=Severity.WARNING,
                message=(f"기준급여 {e.base_salary:,.0f}원(월 5천만 초과) — "
                         f"연봉을 월급여로 잘못 입력했는지 확인하세요."),
                emp_id=e.emp_id,
            )

    # 5) 당년도추계액과 예상(급여×근속) 괴리
    for e in records:
        svc = _years(e.service_start_date(), valuation_date)
        expected = e.base_salary * svc
        if expected > 0 and e.current_year_accrual > 0:
            ratio = e.current_year_accrual / expected
            if ratio > ACCRUAL_RATIO_HIGH or ratio < ACCRUAL_RATIO_LOW:
                report.add(
                    rule="accrual_mismatch", severity=Severity.WARNING,
                    message=(f"당년도추계액이 예상치(급여×근속≈{expected:,.0f}원) 대비 "
                             f"{ratio:.1f}배 — 추계액을 확인하세요."),
                    emp_id=e.emp_id,
                )

    return report
