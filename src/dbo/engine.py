"""PUC 계산 코어 — K-IFRS 1019 예측단위적립방식(Projected Unit Credit).

CLAUDE.md의 "도메인 지식: K-IFRS 1019 PUC 방식" 섹션 수식을 따른다. 모든
timing convention은 config로 제어되며, 기본값은 문서화된 표준 관행이다
(실제 엑셀 결과와의 대사는 프롬프트 4에서 수행하며 이 convention들을 튜닝한다).

────────────────────────────────────────────────────────────────────────────
개인별 계산 모델 (기본 convention)
────────────────────────────────────────────────────────────────────────────
기호:
  x0 = 기준일 도달연령(정수 아님, act/365.25)
  s0 = 기준일 도달근속(service_day_count 기준). 중간정산자는 중간정산기준일부터.
  R  = 정년(종업원구분별), N = 정년까지 투영 연수 = max(1, round(R - x0))
  i  = 할인율, g = 임금상승률, m = 적용배수

미래 연도 t = 1..N 에 대해 (연도 t는 기준일로부터 t-1년 후 ~ t년 후):
  - 탈퇴시점(연): decrement_timing = end_of_year → t,  mid_year → t-0.5
  - 도달근속 S(t) = s0 + 탈퇴시점,  도달연령 = x0 + 탈퇴시점
  - 예상 기준급여 sal(t) = base_salary × (1+g)^e,
      e = salary_increase_timing: start_of_year→t-1, mid_year→t-0.5, end_of_year→t
  - 예상 퇴직급여 B(t) = sal(t) × S(t) × m
  - 재직비율 = s0 / S(t)   (PUC: 현재근속 배분)
  - 할인계수 disc(t) = (1+i)^(-p),  p = discount_timing: end_of_year→t, mid_year→t-0.5

다중탈퇴(연내 사망 먼저, 이후 생존자 중 퇴직/정년):
  q_ret(t)   = 퇴직률(표), 단 t<N. t=N은 정년(전원 퇴직).
  q_death(t) = 사망률(표)
  stay(t)    = (1 - q_death) × (1 - q_ret)         # 연말까지 재직생존
  재직잔존확률 p_start(t) = ∏_{k<t} stay(k),  p_start(1)=1
  당기 사망확률 death_exit(t) = p_start(t) × q_death(t)
  당기 퇴직확률 wd_exit(t)    = p_start(t) × (1-q_death(t)) × q_ret(t)   (t<N)
  정년도달확률 ret_exit(N)    = p_start(N) × (1-q_death(N))              (t=N)
  → 모든 탈퇴에 대해 적립 퇴직급여를 지급한다고 가정.
     exit_prob(t) = death_exit + wd_exit + ret_exit  (합 = 1)

개인 DBO = Σ_t B(t) × (s0/S(t)) × exit_prob(t) × disc(t)
         = Σ_t sal(t) × s0 × m × exit_prob(t) × disc(t)     (S(t) 소거)
개인 CSC (one_year_slice) = Σ_t sal(t) × 1 × m × exit_prob(t) × disc(t)
  (급여가 근속에 선형이므로 attained_minus_prior 방식도 수치상 동일)
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import Config
from .decrement import DecrementTables
from .models import Employee, PlanType

_AGE_BASIS = 365.25  # 도달연령은 act/365.25 고정


def _year_fraction(start: date, end: date, basis: str) -> float:
    """start~end 사이 연수를 근속 일할 방식(basis)으로 계산."""
    if basis == "months":
        months = (end.year - start.year) * 12 + (end.month - start.month)
        if end.day < start.day:
            months -= 1
        return months / 12.0
    days = (end - start).days
    denom = 365.0 if basis == "act/365" else 365.25
    return days / denom


# 탈퇴/급여/할인 timing → 오프셋(연) 매핑
_EXIT_OFFSET = {"end_of_year": 0.0, "mid_year": -0.5}
_SAL_EXP = {"start_of_year": -1.0, "mid_year": -0.5, "end_of_year": 0.0}


@dataclass
class EmployeeResult:
    """개인별 계산 결과."""

    emp_id: str
    emp_class: str
    plan_type: int
    dbo: float
    csc: float
    attained_age: float          # x0
    attained_service: float      # s0
    n_years: int                 # N
    base_salary: float
    current_year_accrual: float
    detail: Optional[pd.DataFrame] = None   # 개인별 연도 상세 (plan_type 1만)


@dataclass
class CensusResult:
    """전체 명부 계산 결과."""

    results: List[EmployeeResult]
    excluded_emp_ids: List[str]           # 제도구분 3 (별도 목록)
    total_dbo: float
    total_csc: float
    subtotal_by_class: Dict[str, Dict[str, float]] = field(default_factory=dict)
    subtotal_by_plan: Dict[int, Dict[str, float]] = field(default_factory=dict)

    def to_dataframe(self) -> pd.DataFrame:
        """개인별 요약 DataFrame (사번·구분·DBO·CSC 등)."""
        return pd.DataFrame(
            [
                {
                    "emp_id": r.emp_id,
                    "emp_class": r.emp_class,
                    "plan_type": r.plan_type,
                    "attained_age": r.attained_age,
                    "attained_service": r.attained_service,
                    "base_salary": r.base_salary,
                    "current_year_accrual": r.current_year_accrual,
                    "DBO": r.dbo,
                    "CSC": r.csc,
                }
                for r in self.results
            ]
        )


def _round(value: float, unit: int) -> float:
    """최종 반올림 (unit 단위, 기본 1원)."""
    if unit and unit > 1:
        return float(round(value / unit) * unit)
    return float(round(value))


def calculate_employee(
    employee: Employee,
    config: Config,
    tables: DecrementTables,
    with_detail: bool = True,
) -> EmployeeResult:
    """종업원 1명의 PUC 상세 테이블과 DBO/CSC를 계산한다.

    제도구분 2(간편법)는 당년도추계액을 그대로 부채로 계상하고 상세를 만들지 않는다.
    제도구분 3(제외)은 이 함수를 호출하지 않는다(calculate_census에서 걸러짐).
    """
    val_date = config.valuation_date
    x0 = _year_fraction(employee.birth_date, val_date, "act/365.25")
    s0 = _year_fraction(employee.service_start_date(), val_date, config.service_day_count)
    plan = int(employee.plan_type.value if isinstance(employee.plan_type, PlanType) else employee.plan_type)
    emp_class = employee.emp_class.value if hasattr(employee.emp_class, "value") else str(employee.emp_class)

    # 제도구분 2: 간편법 — 당년도추계액을 부채로.
    if plan == PlanType.SIMPLIFIED.value:
        dbo = _round(employee.current_year_accrual, config.rounding)
        return EmployeeResult(
            emp_id=employee.emp_id, emp_class=emp_class, plan_type=plan,
            dbo=dbo, csc=0.0, attained_age=x0, attained_service=s0,
            n_years=0, base_salary=employee.base_salary,
            current_year_accrual=employee.current_year_accrual, detail=None,
        )

    R = config.retirement_age.for_class(employee.emp_class)
    N = max(1, int(round(R - x0)))
    m = employee.multiplier

    t = np.arange(1, N + 1, dtype=float)                     # 1..N

    # 탈퇴시점·급여·할인 오프셋
    exit_off = t + _EXIT_OFFSET[config.decrement_timing]      # 탈퇴시점(연)
    S_exit = s0 + exit_off                                    # 도달근속
    age_exit = x0 + exit_off                                  # 도달연령
    sal_exp = t + _SAL_EXP[config.salary_increase_timing]     # 임금상승 지수
    disc_period = t + _EXIT_OFFSET[config.discount_timing]    # 할인기간

    g = config.salary_increase_rate.rate_at()
    sal = employee.base_salary * (1.0 + g) ** sal_exp

    # 할인계수 (기간별 할인율 커브 확장 대비 배열 처리)
    disc_rates = np.array([config.discount_rate.rate_at(float(p)) for p in disc_period])
    disc = (1.0 + disc_rates) ** (-disc_period)

    # 탈퇴율 조회: 연초(연도 t의 시작) 연령/근속 기준
    age_start = x0 + (t - 1)
    svc_start = s0 + (t - 1)
    keys = age_start if config.retirement_rate_basis == "age" else svc_start
    q_ret = tables.retirement_rates(keys, config.retirement_rate_basis)
    q_death = tables.mortality_rates(age_start, employee.gender)

    # 마지막 연도는 정년(전원 퇴직): 퇴직률 테이블 대신 정년 처리로.
    q_ret_wd = q_ret.copy()
    q_ret_wd[-1] = 0.0                                        # t=N 은 '퇴직'이 아니라 '정년'

    stay = (1.0 - q_death) * (1.0 - q_ret_wd)
    stay[-1] = 0.0                                            # 정년 후 잔존 없음
    # p_start(t) = ∏_{k<t} stay(k)
    p_start = np.concatenate(([1.0], np.cumprod(stay)[:-1]))

    death_exit = p_start * q_death
    wd_exit = p_start * (1.0 - q_death) * q_ret_wd            # 당기 퇴직확률 (t<N)
    ret_exit = np.zeros(N)
    ret_exit[-1] = p_start[-1] * (1.0 - q_death[-1])          # 정년도달확률 (t=N)
    exit_prob = death_exit + wd_exit + ret_exit

    accr_ratio = s0 / S_exit
    B = sal * S_exit * m                                      # 예상 퇴직급여
    dbo_contrib = B * accr_ratio * exit_prob * disc           # = sal*s0*m*exit*disc

    # 당기근무원가: 1년치 근속분의 기대현재가치
    if config.csc_method == "attained_minus_prior":
        # DBO(s0+1) - DBO(s0) = sal*1*m*exit*disc (선형이므로 one_year_slice와 동일)
        csc_contrib = sal * 1.0 * m * exit_prob * disc
    else:  # one_year_slice
        csc_contrib = sal * 1.0 * m * exit_prob * disc

    dbo = _round(float(dbo_contrib.sum()), config.rounding)
    csc = _round(float(csc_contrib.sum()), config.rounding)

    detail = None
    if with_detail:
        detail = pd.DataFrame(
            {
                "t": t.astype(int),
                "도달연령": age_exit,
                "도달근속": S_exit,
                "재직잔존확률": p_start,
                "당기퇴직확률": wd_exit,
                "당기사망확률": death_exit,
                "정년도달확률": ret_exit,
                "예상기준급여": sal,
                "예상퇴직급여": B,
                "재직비율": accr_ratio,
                "할인계수": disc,
                "DBO기여분": dbo_contrib,
                "CSC기여분": csc_contrib,
            }
        )

    return EmployeeResult(
        emp_id=employee.emp_id, emp_class=emp_class, plan_type=plan,
        dbo=dbo, csc=csc, attained_age=x0, attained_service=s0, n_years=N,
        base_salary=employee.base_salary,
        current_year_accrual=employee.current_year_accrual, detail=detail,
    )


def calculate_census(
    records: List[Employee],
    config: Config,
    tables: DecrementTables,
    with_detail: bool = False,
) -> CensusResult:
    """전체 명부 계산: 총 DBO/CSC 및 종업원구분별·제도구분별 소계.

    제도구분 3(제외)은 결과에서 빼고 별도 목록(excluded_emp_ids)으로 반환한다.
    with_detail=False(기본)면 개인별 상세 테이블을 생성하지 않아 대량 계산이 빠르다.
    """
    results: List[EmployeeResult] = []
    excluded: List[str] = []

    for emp in records:
        plan = int(emp.plan_type.value if isinstance(emp.plan_type, PlanType) else emp.plan_type)
        if plan == PlanType.EXCLUDED.value:
            excluded.append(emp.emp_id)
            continue
        results.append(calculate_employee(emp, config, tables, with_detail=with_detail))

    total_dbo = float(sum(r.dbo for r in results))
    total_csc = float(sum(r.csc for r in results))

    def _subtotal(key_fn) -> Dict:
        agg: Dict = {}
        for r in results:
            k = key_fn(r)
            slot = agg.setdefault(k, {"count": 0, "DBO": 0.0, "CSC": 0.0})
            slot["count"] += 1
            slot["DBO"] += r.dbo
            slot["CSC"] += r.csc
        return agg

    return CensusResult(
        results=results,
        excluded_emp_ids=excluded,
        total_dbo=total_dbo,
        total_csc=total_csc,
        subtotal_by_class=_subtotal(lambda r: r.emp_class),
        subtotal_by_plan=_subtotal(lambda r: r.plan_type),
    )


def expected_cashflows(
    records: List[Employee],
    config: Config,
    tables: DecrementTables,
) -> pd.DataFrame:
    """만기분석용: 연도별 기대급여지급액(미할인)과 현재가치를 집계한다.

    제도구분 1(DB 정상평가)만 대상. 연도 t의 기대지급액 = Σ_개인 (적립퇴직급여 × 탈퇴확률),
    현재가치 = 기대지급액 × 할인계수 = DBO기여분.
    반환: columns=[연도, 기대급여지급액, 현재가치]  (연도 = 정수 t)
    """
    max_year = 0
    payments: Dict[int, float] = {}
    pvs: Dict[int, float] = {}
    for emp in records:
        plan = int(emp.plan_type.value if isinstance(emp.plan_type, PlanType) else emp.plan_type)
        if plan != PlanType.DB_NORMAL.value:
            continue
        res = calculate_employee(emp, config, tables, with_detail=True)
        d = res.detail
        if d is None:
            continue
        pv = d["DBO기여분"].to_numpy()
        disc = d["할인계수"].to_numpy()
        cf = np.where(disc > 0, pv / disc, 0.0)              # 미할인 기대지급액
        for yr, c, p in zip(d["t"].to_numpy(), cf, pv):
            yr = int(yr)
            payments[yr] = payments.get(yr, 0.0) + float(c)
            pvs[yr] = pvs.get(yr, 0.0) + float(p)
            max_year = max(max_year, yr)

    rows = [
        {"연도": yr, "기대급여지급액": payments.get(yr, 0.0), "현재가치": pvs.get(yr, 0.0)}
        for yr in range(1, max_year + 1)
    ]
    return pd.DataFrame(rows)


def weighted_average_duration(cashflows: pd.DataFrame) -> float:
    """DBO 가중평균만기(듀레이션) = Σ 연도×현재가치 / Σ 현재가치."""
    if cashflows.empty:
        return 0.0
    pv = cashflows["현재가치"].to_numpy()
    yr = cashflows["연도"].to_numpy()
    total = pv.sum()
    return float((yr * pv).sum() / total) if total else 0.0


def dump_employee_detail(
    employee: Employee,
    config: Config,
    tables: DecrementTables,
    out_path: str,
) -> Optional[str]:
    """debug 모드: 지정 사번의 개인별 상세 테이블을 CSV로 덤프한다.

    엑셀 한글 호환을 위해 utf-8-sig로 저장. 제도구분 2/3은 상세가 없어 None 반환.
    """
    result = calculate_employee(employee, config, tables, with_detail=True)
    if result.detail is None:
        return None
    result.detail.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path
