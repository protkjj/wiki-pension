"""할인율 커브 → 듀레이션 반영 단일할인율 산출.

K-IFRS 1019에서 할인율은 우량회사채(AA) 만기별 수익률(spot 커브)을 쓰되,
실무에서는 부채의 현금흐름 프로파일에 대해 **커브로 계산한 부채 현재가치와
동일한 현재가치를 주는 단일(flat) 할인율**을 구해 적용한다. 이 단일율이
'듀레이션이 반영된 할인율'이다.

  단일할인율 r*  s.t.  PV_flat(현금흐름, r*) == PV_curve(현금흐름, spot커브)

시점 convention은 엔진과 정합(mid_year: t-0.5, end_of_year: t).
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple


def _exponent(year: int, timing: str) -> float:
    """할인 지수(년). mid_year면 연중(t-0.5), end_of_year면 연말(t)."""
    return (year - 0.5) if timing == "mid_year" else float(year)


def _spot_for_period(curve: Dict[int, float], period: float) -> float:
    """할인기간(period, 실수)에 적용할 spot rate.

    실무 엑셀 관행과 정합: 기간 p의 현금흐름은 **floor(p) 정수 만기**의 spot을 쓴다
    (예: 1.5년 → 1년물, 2.5년 → 2년물). 커브 최소/최대 만기로 clamp.
    (선형보간이 아니라 '해당 연도 구간의 만기 금리'를 그대로 적용하는 방식)
    """
    m = int(math.floor(period))
    lo, hi = min(curve), max(curve)
    m = max(lo, min(hi, m))
    return curve[m] if m in curve else interpolate_curve(curve, float(m))


def curve_pv(cashflows: Sequence[Tuple[int, float]], curve: Dict[int, float],
             timing: str = "mid_year") -> float:
    """만기별 spot 커브로 할인한 현금흐름 현재가치."""
    tot = 0.0
    for yr, cf in cashflows:
        p = _exponent(yr, timing)
        tot += cf / (1 + _spot_for_period(curve, p)) ** p
    return tot


def flat_pv(cashflows: Sequence[Tuple[int, float]], rate: float,
            timing: str = "mid_year") -> float:
    """단일(flat) 할인율로 할인한 현금흐름 현재가치."""
    return sum(cf / (1 + rate) ** _exponent(yr, timing) for yr, cf in cashflows)


def weighted_duration(cashflows: Sequence[Tuple[int, float]], curve: Dict[int, float],
                      timing: str = "mid_year") -> float:
    """가중평균만기(듀레이션) = Σ 시점 × 현금흐름 / Σ 현금흐름 (미할인 현금흐름 가중).

    실무 엑셀 관행과 정합(현가 가중이 아니라 예상지급 현금흐름 가중의 평균 만기).
    """
    num = den = 0.0
    for yr, cf in cashflows:
        t = _exponent(yr, timing)
        num += t * cf
        den += cf
    return num / den if den else 0.0


def single_equivalent_rate(cashflows: Sequence[Tuple[int, float]], curve: Dict[int, float],
                           timing: str = "mid_year", tol: float = 1e-10) -> float:
    """커브 PV와 같아지는 단일할인율 r*를 이분법으로 찾는다.

    PV_flat(r)는 r에 대해 단조감소 → 이분법으로 안정적으로 수렴한다.
    """
    target = curve_pv(cashflows, curve, timing)
    lo, hi = 0.0, 0.30
    for _ in range(200):
        mid = (lo + hi) / 2
        if flat_pv(cashflows, mid, timing) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def interpolate_curve(curve: Dict[int, float], t: float) -> float:
    """만기 t(실수)에서 커브 선형보간 — 듀레이션 시점의 참조 할인율."""
    if t <= min(curve):
        return curve[min(curve)]
    if t >= max(curve):
        return curve[max(curve)]
    lo = max(m for m in curve if m <= t)
    hi = min(m for m in curve if m >= t)
    if lo == hi:
        return curve[lo]
    w = (t - lo) / (hi - lo)
    return curve[lo] * (1 - w) + curve[hi] * w


def solve(cashflows: Sequence[Tuple[int, float]], curve: Dict[int, float],
          timing: str = "mid_year") -> Dict[str, float]:
    """단일할인율·듀레이션·듀레이션할인율·커브부채PV를 한번에 산출."""
    pv = curve_pv(cashflows, curve, timing)
    dur = weighted_duration(cashflows, curve, timing)
    single = single_equivalent_rate(cashflows, curve, timing)
    return {
        "curve_pv": pv,
        "duration": dur,
        "single_rate": single,
        "duration_rate": interpolate_curve(curve, dur),
    }
