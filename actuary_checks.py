"""계리사 표준 명부 오류검증 규칙 (재직자명부).

고객사 계리사의 'error check' 워크시트 규칙을 반영한다. 현재 표준 스키마
(사번·생년월일·성별·입사일·기준급여·당년도추계액·종업원구분·중간정산일·
중간정산액·제도구분·적용배수)로 판정 가능한 규칙을 구현한다.

원본 규칙 ↔ 구현 매핑 (재직자명부, 단일파일로 판정 가능한 규칙 — 적용됨):
  R1  당년도추계액 < 0            → census.validate_census (accrual_negative)
  R2  차년도추계액 < 0            → next_accrual_negative (차년도 컬럼 제공 시)
  R3  중간정산액 < 0              → interim_amount_negative (ERROR)
  R4  입사연령 <17 or >70         → hire_age_range (ERROR)
  R5  입사일 < 생년월일           → census (birth_after_hire)
  R6  중간정산일 <= 입사일        → census (interim_before_hire)
  R7  시산일 <= 입사일            → census (hire_after_valuation)
  R8  시산일 <= 중간정산일        → census (interim_after_valuation)
  R12 기준급여 < 하한             → salary_below_min / smart_checks
  R13~17 blank(사번/생년월일/…)   → census 필수·파싱 검증 (missing_columns/parse)
  R21 연령>정년 & 당년도추계액 0  → over_age_zero_accrual
  R22 연령>정년 & 차년도추계액 0  → over_age_zero_next_accrual (차년도 컬럼 제공 시)
  R23 차년도-당년도 > 2×기준급여  → accrual_jump
  R24/25 중간정산 날짜·금액 짝    → census (interim_amount/date_missing)
  R18 직종>2 & 차년도<당년도추계액 → exec_next_lt_current (WARNING)
  R19 직종>2 & 당년도추계액 0/누락 → exec_zero_current_accrual (WARNING)
  R20 직종>2 & 차년도추계액 0/누락 → exec_zero_next_accrual (WARNING, 차년도 컬럼 제공 시)
  R26 직종 코드 범위             → models 값매핑(1/3/4/6/7 등) 밖 코드는 parse 오류
  R27 입사연령 > 정년             → hire_age_over_retirement (WARNING, 임원 제외)
  R28 IFRS 가입 Y/N               → ifrs_invalid (IFRS 컬럼 제공 시)
  R29 중간정산금액 < 하한         → interim_amount_below_min (WARNING)
  R31 재직자 중복(사번)           → smart_checks (duplicate_emp_id)
  (+) 도달연령 > 정년 재직자      → attained_age_over_retirement (WARNING, 임원 제외)

⚠️ 아직 미적용 — 추가 자료/명부가 필요한 규칙(별도 구현 대상):
  R32~42, 기준급여 ±20% 변동 등  → 전기(전년도) 명부 대비 비교 필요.
                                    전기말재직자명부는 업로드되나 대사 로직 미구현.
  퇴직자·DC전환/전출입/3년명부    → 업로드·PII제거만 하고 명부별 검증 규칙 미구현.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from .census import Severity, ValidationReport
from .models import EmpClass, Employee

DEFAULT_SALARY_MIN = 1_000_000       # 기준급여 하한(백만). 고객 파일 2025판은 1,700,000
DEFAULT_INTERIM_MIN = 1_000_000      # 중간정산금액 하한
MIN_HIRE_AGE = 17                    # 입사연령 하한
MAX_HIRE_AGE = 70                    # 입사연령 상한
DEFAULT_RETIREMENT_AGE = 60          # 일반/계약직 정년(회사 미지정 시)


def _years(a: date, b: date) -> float:
    return (b - a).days / 365.25


def run_actuary_checks(
    records,
    valuation_date: date,
    report: Optional[ValidationReport] = None,
    config=None,
    salary_min: float = DEFAULT_SALARY_MIN,
    interim_min: float = DEFAULT_INTERIM_MIN,
    min_hire_age: int = MIN_HIRE_AGE,
    max_hire_age: int = MAX_HIRE_AGE,
    retirement_age: int = DEFAULT_RETIREMENT_AGE,
    exec_retirement_age: Optional[int] = None,
) -> ValidationReport:
    """계리사 표준 재직자 규칙을 적용한다(중복 규칙은 census/smart에서 처리).

    정년 규칙:
      - 일반직·계약직은 `retirement_age`(회사 지정, 기본 60)로 정년 검증.
      - 임원(EXECUTIVE)은 정년 검증 제외가 원칙. 단, `exec_retirement_age`가
        지정되면 그 값으로 검증한다.
      - config가 주어지면 config.retirement_age(구분별)를 우선한다.
    """
    if report is None:
        report = ValidationReport(n_records=len(records))

    def _ret_age(emp_class) -> Optional[int]:
        """해당 구분의 정년. None이면 정년 검증 제외(임원 기본)."""
        if config is not None:
            return config.retirement_age.for_class(emp_class)
        if emp_class == EmpClass.EXECUTIVE:
            return exec_retirement_age if (exec_retirement_age or 0) > 0 else None
        return retirement_age

    # 컬럼 제공 여부(전원 None이면 해당 컬럼 미제공 → 관련 규칙 skip)
    has_next = any(e.next_year_accrual is not None for e in records)
    has_ifrs = any(e.ifrs_enrolled not in (None, "") for e in records)

    for e in records:  # type: Employee
        eid = e.emp_id
        age_at_hire = _years(e.birth_date, e.hire_date)
        attained_age = _years(e.birth_date, valuation_date)
        ra_c = _ret_age(e.emp_class)   # None이면 임원 등 정년 검증 제외

        # R4: 입사연령 < 17 or > 70
        if age_at_hire < min_hire_age or age_at_hire > max_hire_age:
            report.add(
                rule="hire_age_range", severity=Severity.ERROR,
                message=(f"입사연령 {age_at_hire:.1f}세 (허용 {min_hire_age}~{max_hire_age}세) "
                         f"— 생년월일/입사일을 확인하세요."),
                emp_id=eid,
            )

        # R27: 입사연령 > 정년 (임원 등 정년 미적용자는 skip).
        #   정년 이후 입사(촉탁·재입사 등)는 정상일 수 있어 '경고'로 플래그 후 진행한다.
        if ra_c is not None and age_at_hire > ra_c:
            report.add(
                rule="hire_age_over_retirement", severity=Severity.WARNING,
                message=(f"입사연령 {age_at_hire:.1f}세가 정년 {ra_c}세를 초과합니다 "
                         f"— 촉탁/재입사 여부와 산출방식을 확인하세요."),
                emp_id=eid,
            )

        # ★ 정년 초과 재직자: 도달연령 > 정년인데 아직 재직 중 (임원 제외).
        #   정년퇴직 누락·정년연장·촉탁 여부와 산출방식(정년초과자 처리)을 확인해야 함.
        if ra_c is not None and attained_age > ra_c:
            report.add(
                rule="attained_age_over_retirement", severity=Severity.WARNING,
                message=(f"도달연령 {attained_age:.1f}세가 정년 {ra_c}세를 초과한 재직자입니다 "
                         f"— 정년연장/촉탁 여부와 정년초과자 산출방식을 확인하세요."),
                emp_id=eid,
            )

        # R3: 중간정산액 < 0
        if e.interim_settlement_amount is not None and e.interim_settlement_amount < 0:
            report.add(
                rule="interim_amount_negative", severity=Severity.ERROR,
                message=f"중간정산액이 음수입니다 ({e.interim_settlement_amount:,.0f}).",
                emp_id=eid,
            )

        # R29: 중간정산금액 < 하한
        if e.interim_settlement_amount is not None and 0 < e.interim_settlement_amount < interim_min:
            report.add(
                rule="interim_amount_below_min", severity=Severity.WARNING,
                message=(f"중간정산액 {e.interim_settlement_amount:,.0f}원이 하한 "
                         f"{interim_min:,.0f}원 미만 — 단위를 확인하세요."),
                emp_id=eid,
            )

        # R12: 기준급여 < 하한 (계리사 표준 임계값)
        if 0 < e.base_salary < salary_min:
            report.add(
                rule="salary_below_min", severity=Severity.WARNING,
                message=(f"기준급여 {e.base_salary:,.0f}원이 하한 {salary_min:,.0f}원 미만 "
                         f"— 단위/최저임금을 확인하세요."),
                emp_id=eid,
            )

        # R2: 차년도추계액 < 0
        if e.next_year_accrual is not None and e.next_year_accrual < 0:
            report.add(
                rule="next_accrual_negative", severity=Severity.ERROR,
                message=f"차년도추계액이 음수입니다 ({e.next_year_accrual:,.0f}).", emp_id=eid,
            )

        # R23: 차년도추계액 - 당년도추계액 > 2 × 기준급여
        if e.next_year_accrual is not None:
            jump = e.next_year_accrual - e.current_year_accrual
            if jump > 2 * e.base_salary:
                report.add(
                    rule="accrual_jump", severity=Severity.WARNING,
                    message=(f"차년도추계액-당년도추계액({jump:,.0f})이 기준급여의 2배 "
                             f"({2*e.base_salary:,.0f})를 초과 — 확인하세요."),
                    emp_id=eid,
                )

        # R21: 연령>정년인데 당년도추계액이 0/blank (임원 등 정년 미적용자 skip)
        if ra_c is not None and attained_age > ra_c and (e.current_year_accrual in (None, 0)):
            report.add(
                rule="over_age_zero_accrual", severity=Severity.WARNING,
                message=f"연령 {attained_age:.0f}세가 정년 {ra_c} 초과인데 당년도추계액이 0/누락.",
                emp_id=eid,
            )

        # R22: 연령>정년인데 차년도추계액이 0/blank (차년도 컬럼 제공 시)
        if has_next and ra_c is not None and attained_age > ra_c and (e.next_year_accrual in (None, 0)):
            report.add(
                rule="over_age_zero_next_accrual", severity=Severity.WARNING,
                message=f"연령 {attained_age:.0f}세가 정년 {ra_c} 초과인데 차년도추계액이 0/누락.",
                emp_id=eid,
            )

        # R28: IFRS 가입 값이 Y/N이 아님 (컬럼 제공 시)
        if has_ifrs and e.ifrs_enrolled is not None and str(e.ifrs_enrolled).strip() not in ("Y", "N"):
            report.add(
                rule="ifrs_invalid", severity=Severity.ERROR,
                message=f"IFRS 가입 값이 Y/N이 아닙니다 ('{e.ifrs_enrolled}').", emp_id=eid,
            )

        # 직종>2(임원·계약직) 전용 추계액 규칙 — 이들은 추계액이 필수 입력.
        #   원본 error check 시트 Y/Z/AA열(직종코드>2 조건)에 대응.
        if e.emp_class in (EmpClass.EXECUTIVE, EmpClass.CONTRACT):
            # #18(Y): 차년도추계액 < 당년도추계액 (둘 다 입력된 경우)
            if e.next_year_accrual is not None and e.next_year_accrual < e.current_year_accrual:
                report.add(
                    rule="exec_next_lt_current", severity=Severity.WARNING,
                    message=(f"임원·계약직인데 차년도추계액({e.next_year_accrual:,.0f})이 "
                             f"당년도추계액({e.current_year_accrual:,.0f})보다 작습니다 — 확인하세요."),
                    emp_id=eid,
                )
            # #19(Z): 당년도추계액이 0/누락
            if e.current_year_accrual in (None, 0):
                report.add(
                    rule="exec_zero_current_accrual", severity=Severity.WARNING,
                    message="임원·계약직인데 당년도추계액이 0/누락 — 임원·계약직은 추계액 필수 입력.",
                    emp_id=eid,
                )
            # #20(AA): 차년도추계액이 0/누락 (차년도 컬럼 제공 시)
            if has_next and (e.next_year_accrual in (None, 0)):
                report.add(
                    rule="exec_zero_next_accrual", severity=Severity.WARNING,
                    message="임원·계약직인데 차년도추계액이 0/누락 — 임원·계약직은 추계액 필수 입력.",
                    emp_id=eid,
                )

    return report


def run_aux_cross_checks(records, retiree_ids=None, transfer=None,
                         report: Optional[ValidationReport] = None) -> ValidationReport:
    """보조 명부(퇴직자·전출입) 대비 재직자명부 교차검증.

    원본 error check 워크시트 '퇴직자명부'·'전출입명부' 규칙 대응:
      - 재직자 ∩ 퇴직자 사번 중복(오류): 퇴직자는 재직명부에서 제외해야 함
      - 전출입: 전입·전출 동시 사번(경고), 전출자가 재직명부에 남음(경고),
                퇴직자∩전출입 중복(경고)
    retiree_ids: 퇴직자·DC전환명부 사번 목록
    transfer: [(사번, 사유코드)] — 1전입 2전출 3사업결합 4사업처분 5기타장기
    """
    if report is None:
        report = ValidationReport(n_records=len(records))
    active_ids = {e.emp_id for e in records}
    retiree_set = set(retiree_ids or [])
    transfer = transfer or []

    for eid in sorted(active_ids & retiree_set):
        report.add(
            rule="active_retiree_dup", severity=Severity.ERROR,
            message="재직자명부와 퇴직자명부에 사번이 중복됩니다 — 퇴직자는 재직명부에서 제외하세요.",
            emp_id=eid,
        )

    ins = {eid for eid, r in transfer if r in (1, 3)}    # 전입·사업결합
    outs = {eid for eid, r in transfer if r in (2, 4)}   # 전출·사업처분
    transfer_ids = {eid for eid, _ in transfer}
    for eid in sorted(ins & outs):
        report.add(
            rule="transfer_in_out_dup", severity=Severity.WARNING,
            message="전출입명부에 전입·전출이 동시에 있는 사번입니다 — 확인하세요.", emp_id=eid,
        )
    for eid in sorted(outs & active_ids):
        report.add(
            rule="transfer_out_still_active", severity=Severity.WARNING,
            message="전출(관계사전출/사업처분) 사번이 재직자명부에 남아있습니다 — 확인하세요.",
            emp_id=eid,
        )
    for eid in sorted(retiree_set & transfer_ids):
        report.add(
            rule="retiree_transfer_dup", severity=Severity.WARNING,
            message="퇴직자명부와 전출입명부에 사번이 중복됩니다 — 확인하세요.", emp_id=eid,
        )
    return report


def run_cross_year_checks(current, prior, retiree_ids=None,
                          report: Optional[ValidationReport] = None,
                          salary_change_pct: float = 0.20) -> ValidationReport:
    """전기말재직자명부 대비 당기 재직자명부 교차검증(손익원천분석 전 데이터 점검).

    원본 error check 워크시트 재직자 #25~#29 대응:
      - 사번 동일인데 생년월일·성별·입사일·중간정산일 불일치(경고)
      - 기준급여 전년대비 ±20% 초과 변동(경고)
      - 전년도 재직자가 당기 명부·퇴직자명부에 없음(경고, 퇴직/전출 처리 확인)
    """
    if report is None:
        report = ValidationReport(n_records=len(current))
    prior_by = {e.emp_id: e for e in prior}
    cur_ids = {e.emp_id for e in current}
    retiree = set(retiree_ids or [])
    pct = int(salary_change_pct * 100)

    for e in current:
        p = prior_by.get(e.emp_id)
        if p is None:
            continue  # 신입 — 정상
        if e.birth_date != p.birth_date:
            report.add(rule="prior_birth_mismatch", severity=Severity.WARNING,
                       message=f"전년도 생년월일({p.birth_date})과 당기({e.birth_date})가 다릅니다 — 확인.",
                       emp_id=e.emp_id)
        if e.gender != p.gender:
            report.add(rule="prior_gender_mismatch", severity=Severity.WARNING,
                       message="전년도 성별과 당기 성별이 다릅니다 — 확인.", emp_id=e.emp_id)
        if e.hire_date != p.hire_date:
            report.add(rule="prior_hire_mismatch", severity=Severity.WARNING,
                       message=f"전년도 입사일({p.hire_date})과 당기({e.hire_date})가 다릅니다 — 확인.",
                       emp_id=e.emp_id)
        if (e.interim_settlement_date or None) != (p.interim_settlement_date or None):
            report.add(rule="prior_interim_mismatch", severity=Severity.WARNING,
                       message="전년도 중간정산일과 당기 중간정산일이 다릅니다 — 확인.", emp_id=e.emp_id)
        if p.base_salary and p.base_salary > 0:
            chg = (e.base_salary - p.base_salary) / p.base_salary
            if abs(chg) >= salary_change_pct:
                report.add(rule="prior_salary_change", severity=Severity.WARNING,
                           message=(f"기준급여가 전년대비 {chg*100:+.1f}% 변동(±{pct}% 초과) — "
                                    f"전년 {p.base_salary:,.0f} → 당기 {e.base_salary:,.0f}, 확인."),
                           emp_id=e.emp_id)

    for p in prior:
        if p.emp_id not in cur_ids and p.emp_id not in retiree:
            report.add(rule="prior_active_missing", severity=Severity.WARNING,
                       message="전년도 재직자가 당기 명부·퇴직자명부에 없습니다 — 퇴직/전출 처리를 확인하세요.",
                       emp_id=p.emp_id)
    return report
