"""actuary_checks.py 테스트: 계리사 표준 재직자 규칙."""

from datetime import date

from dbo.actuary_checks import run_actuary_checks
from dbo.census import Severity
from dbo.config import Config
from dbo.models import Employee

VAL = date(2025, 12, 31)


def _emp(emp_id="1", birth=date(1985, 1, 1), hire=date(2010, 1, 1), salary=3_000_000,
         interim_amount=None, interim_date=None, emp_class="REGULAR",
         accrual=10_000_000, next_accrual=None, ifrs=None):
    return Employee(
        emp_id=emp_id, birth_date=birth, gender="M", hire_date=hire,
        base_salary=salary, current_year_accrual=accrual, emp_class=emp_class,
        next_year_accrual=next_accrual, ifrs_enrolled=ifrs,
        interim_settlement_date=interim_date, interim_settlement_amount=interim_amount,
    )


def _rules(rep, sev=None):
    return [i.rule for i in rep.issues if sev is None or i.severity == sev]


def test_hire_age_range_error():
    # 2005년생이 2015년 입사 → 입사연령 약 10세(<17)
    rep = run_actuary_checks([_emp(birth=date(2005, 1, 1), hire=date(2015, 1, 1))], VAL)
    assert "hire_age_range" in _rules(rep, Severity.ERROR)


def test_hire_age_over_retirement_warns():
    # 1950년생이 2015년 입사 → 입사연령 약 65세 > 정년 60 (촉탁/재입사 가능 → 경고)
    rep = run_actuary_checks([_emp(birth=date(1950, 1, 1), hire=date(2015, 1, 1))], VAL)
    assert "hire_age_over_retirement" in _rules(rep, Severity.WARNING)


def test_attained_age_over_retirement_warns_excludes_executive():
    # 정년초과 재직자(도달연령>정년)는 경고, 임원은 제외.
    reg = run_actuary_checks([_emp(birth=date(1958, 1, 1))], VAL)  # 약 67세, REGULAR
    assert "attained_age_over_retirement" in _rules(reg, Severity.WARNING)
    exe = run_actuary_checks([_emp(birth=date(1958, 1, 1), emp_class="EXECUTIVE")], VAL)
    assert "attained_age_over_retirement" not in _rules(exe)  # 임원 정년 미적용
    # 임원정년 지정 시에는 임원도 검증
    exe2 = run_actuary_checks([_emp(birth=date(1958, 1, 1), emp_class="EXECUTIVE")], VAL,
                              exec_retirement_age=65)
    assert "attained_age_over_retirement" in _rules(exe2, Severity.WARNING)


def test_interim_amount_negative_error():
    rep = run_actuary_checks([_emp(interim_amount=-100, interim_date=date(2018, 1, 1))], VAL)
    assert "interim_amount_negative" in _rules(rep, Severity.ERROR)


def test_interim_amount_below_min_warns():
    rep = run_actuary_checks([_emp(interim_amount=500_000, interim_date=date(2018, 1, 1))], VAL)
    assert "interim_amount_below_min" in _rules(rep, Severity.WARNING)


def test_salary_below_min_warns_with_threshold():
    # 기준값 1,700,000으로 상향 시 160만 급여가 하한 미만
    rep = run_actuary_checks([_emp(salary=1_600_000)], VAL, salary_min=1_700_000)
    assert "salary_below_min" in _rules(rep, Severity.WARNING)


def test_retirement_age_from_config_by_class():
    # 임원 정년 65: 입사연령 63세면 통과, 66세면 초과
    cfg = Config.from_dict({
        "valuation_date": "2025-12-31", "discount_rate": 0.045, "salary_increase_rate": 0.03,
        "retirement_age": {"default": 60, "by_class": {"EXECUTIVE": 65}},
    })
    ok = run_actuary_checks([_emp(birth=date(1955, 1, 1), hire=date(2016, 1, 1), emp_class="EXECUTIVE")],
                            VAL, config=cfg)  # 약 61세 입사 < 65
    assert "hire_age_over_retirement" not in _rules(ok, Severity.ERROR)


def test_exec_accrual_rules_for_over_class():
    # 직종>2(임원·계약직): 차년도<당년도, 당년도 0, 차년도 0 각각 경고.
    e1 = _emp(emp_class="EXECUTIVE", accrual=100, next_accrual=50)   # 차년도<당년도
    r1 = run_actuary_checks([e1], VAL)
    assert "exec_next_lt_current" in _rules(r1, Severity.WARNING)

    e2 = _emp(emp_class="CONTRACT", accrual=0, next_accrual=100)     # 당년도 0
    r2 = run_actuary_checks([e2], VAL)
    assert "exec_zero_current_accrual" in _rules(r2, Severity.WARNING)

    e3 = _emp(emp_class="EXECUTIVE", accrual=100, next_accrual=0)    # 차년도 0
    r3 = run_actuary_checks([e3], VAL)
    assert "exec_zero_next_accrual" in _rules(r3, Severity.WARNING)

    # 일반직(REGULAR, 직종<=2)은 이 규칙 대상 아님
    e4 = _emp(emp_class="REGULAR", accrual=100, next_accrual=50)
    r4 = run_actuary_checks([e4], VAL)
    assert "exec_next_lt_current" not in _rules(r4)


def test_next_accrual_negative_error():
    rep = run_actuary_checks([_emp(next_accrual=-1)], VAL)
    assert "next_accrual_negative" in _rules(rep, Severity.ERROR)


def test_accrual_jump_warns():
    # 차년도-당년도 = 1억 > 2×기준급여(600만)
    rep = run_actuary_checks([_emp(accrual=10_000_000, next_accrual=110_000_000, salary=3_000_000)], VAL)
    assert "accrual_jump" in _rules(rep, Severity.WARNING)


def test_ifrs_invalid_error_only_when_provided():
    # 컬럼 제공 + 잘못된 값 → 오류
    rep = run_actuary_checks([_emp(ifrs="X")], VAL)
    assert "ifrs_invalid" in _rules(rep, Severity.ERROR)
    # 정상 값이면 오류 없음
    rep2 = run_actuary_checks([_emp(ifrs="Y")], VAL)
    assert "ifrs_invalid" not in _rules(rep2, Severity.ERROR)
    # 컬럼 미제공(전원 None)이면 규칙 skip
    rep3 = run_actuary_checks([_emp(ifrs=None)], VAL)
    assert "ifrs_invalid" not in _rules(rep3)


def test_over_age_zero_next_accrual_gated_on_column():
    # 정년 초과 + 차년도추계액 0, 단 차년도 컬럼이 제공된 경우만 경고
    old = _emp(birth=date(1958, 1, 1), next_accrual=0, accrual=5_000_000)  # 약 67세
    rep = run_actuary_checks([old], VAL)
    assert "over_age_zero_next_accrual" in _rules(rep, Severity.WARNING)
    # 차년도 컬럼 미제공(None)이면 skip
    old2 = _emp(birth=date(1958, 1, 1), next_accrual=None, accrual=5_000_000)
    rep2 = run_actuary_checks([old2], VAL)
    assert "over_age_zero_next_accrual" not in _rules(rep2)


def test_clean_passes():
    rep = run_actuary_checks([_emp()], VAL)
    assert not rep.has_errors


def test_aux_cross_checks():
    from dbo.actuary_checks import run_aux_cross_checks
    active = [_emp(emp_id="A1"), _emp(emp_id="A2"), _emp(emp_id="OUT1")]
    # A1은 퇴직자명부에도 있음 → 오류. OUT1은 전출인데 재직중 → 경고. T1은 전입/전출 동시 → 경고.
    rep = run_aux_cross_checks(active, retiree_ids=["A1", "R9"],
                               transfer=[("OUT1", 2), ("T1", 1), ("T1", 2), ("R9", 1)])
    rules = {i.rule for i in rep.issues}
    assert "active_retiree_dup" in rules          # A1
    assert "transfer_out_still_active" in rules    # OUT1
    assert "transfer_in_out_dup" in rules          # T1
    assert "retiree_transfer_dup" in rules         # R9 (퇴직∩전출입)
    # A1 재직∩퇴직은 ERROR
    assert any(i.rule == "active_retiree_dup" and i.severity == Severity.ERROR for i in rep.issues)


def test_cross_year_checks():
    from dbo.actuary_checks import run_cross_year_checks
    cur = [
        _emp(emp_id="S1", salary=3_000_000),                       # 급여 유지
        _emp(emp_id="S2", salary=4_000_000),                       # 전년 3M→4M = +33% 변동
        _emp(emp_id="S3", birth=date(1990, 1, 1)),                 # 전년 생년월일 다름
    ]
    prior = [
        _emp(emp_id="S1", salary=3_000_000),
        _emp(emp_id="S2", salary=3_000_000),
        _emp(emp_id="S3", birth=date(1985, 1, 1)),
        _emp(emp_id="P9"),                                         # 전년 재직, 당기 없음(누락)
    ]
    rep = run_cross_year_checks(cur, prior, retiree_ids=[])
    rules = {i.rule for i in rep.issues}
    assert "prior_salary_change" in rules       # S2
    assert "prior_birth_mismatch" in rules      # S3
    assert "prior_active_missing" in rules      # P9
    # P9가 퇴직자명부에 있으면 누락 경고 없음
    rep2 = run_cross_year_checks(cur, prior, retiree_ids=["P9"])
    assert not any(i.rule == "prior_active_missing" and i.emp_id == "P9" for i in rep2.issues)
