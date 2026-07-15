"""smart_checks.py 테스트: 통계·논리 이상 검출."""

from datetime import date

from dbo.census import Severity, ValidationReport
from dbo.models import Employee
from dbo.smart_checks import run_smart_checks

VAL = date(2025, 12, 31)


def _emp(emp_id, birth, hire, salary=3_000_000, accrual=None, emp_class="REGULAR"):
    return Employee(
        emp_id=emp_id, birth_date=birth, gender="M", hire_date=hire,
        base_salary=salary,
        current_year_accrual=(salary * 10 if accrual is None else accrual),
        emp_class=emp_class,
    )


def _rules(report, severity=None):
    return [i.rule for i in report.issues if severity is None or i.severity == severity]


def test_duplicate_emp_id_is_error():
    recs = [_emp("100", date(1985, 1, 1), date(2010, 1, 1)),
            _emp("100", date(1990, 1, 1), date(2015, 1, 1))]
    rep = run_smart_checks(recs, VAL)
    assert "duplicate_emp_id" in _rules(rep, Severity.ERROR)


def test_salary_outlier_flagged_against_peers():
    # 정규직 9명 300만 + 1명 5억 → 이상치 경고
    recs = [_emp(str(i), date(1985, 1, 1), date(2010, 1, 1), salary=3_000_000) for i in range(9)]
    recs.append(_emp("X", date(1985, 1, 1), date(2010, 1, 1), salary=500_000_000))
    rep = run_smart_checks(recs, VAL)
    outliers = [i for i in rep.issues if i.rule == "salary_outlier"]
    assert any(i.emp_id == "X" for i in outliers)


def test_salary_absolute_high_bound():
    # 상한 초과만 smart_checks에서 처리(하한은 actuary_checks 담당)
    recs = [_emp("hi", date(1985, 1, 1), date(2010, 1, 1), salary=80_000_000)]  # 월 8천만
    rep = run_smart_checks(recs, VAL)
    assert "salary_too_high" in _rules(rep, Severity.WARNING)


def test_accrual_mismatch_warns():
    # 급여 300만 × 근속 약 15년 ≈ 4,500만 예상인데 추계액 5억 → 괴리
    recs = [_emp("1", date(1980, 1, 1), date(2010, 12, 31), salary=3_000_000, accrual=500_000_000)]
    rep = run_smart_checks(recs, VAL)
    assert "accrual_mismatch" in _rules(rep, Severity.WARNING)


def test_clean_records_minimal_issues():
    # 정상 데이터는 이상 없음(중복·나이·이상치 모두 통과)
    recs = [_emp(str(i), date(1985, 1, 1), date(2010, 1, 1), salary=3_000_000 + i * 1000)
            for i in range(10)]
    rep = run_smart_checks(recs, VAL)
    assert not rep.has_errors
