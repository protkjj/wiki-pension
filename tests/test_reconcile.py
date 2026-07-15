"""reconcile.py 테스트: 개인별 비교, 허용오차, convention 탐색 추천."""

from datetime import date

import pandas as pd
import pytest

from dbo.config import Config
from dbo.decrement import DecrementTables
from dbo.engine import calculate_census
from dbo.models import Employee
from dbo.reconcile import compare_dbo, result_to_dbo_table, sweep_conventions


# ---------------------------------------------------------------------------
# 1) 개인별 비교
# ---------------------------------------------------------------------------


def test_compare_dbo_matches_diffs_and_one_sided():
    engine = pd.DataFrame(
        {"emp_id": ["1", "2", "3", "4"], "dbo": [1_000_000, 2_000_100, 3_000_000, 500]}
    )
    excel = pd.DataFrame(
        {"emp_id": ["1", "2", "3", "5"], "dbo": [1_000_000, 2_000_000, 3_500_000, 900]}
    )
    cmp = compare_dbo(engine, excel, abs_tol=1.0, rel_tol=0.0001)

    s = cmp.summary
    assert s["n_common"] == 3
    # emp1: 정확일치, emp2: 차이100이지만 상대오차 0.005% ≤ 0.01% → 일치
    # emp3: 큰 차이 → 불일치
    assert s["within_count"] == 2
    assert s["within_rate"] == pytest.approx(2 / 3)

    # 편측 사번
    assert cmp.only_in_engine == ["4"]
    assert cmp.only_in_excel == ["5"]

    # 총액 차이 = 공통 3명 합 차이
    assert s["total_diff"] == pytest.approx(6_000_100 - 6_500_000)

    # 차이 상위 1위는 emp3 (절대차 최대)
    assert cmp.top_diff.iloc[0]["emp_id"] == "3"


def test_compare_dbo_within_by_absolute_tolerance():
    engine = pd.DataFrame({"emp_id": ["1"], "dbo": [1000]})
    excel = pd.DataFrame({"emp_id": ["1"], "dbo": [1000.5]})  # 0.5원 차이
    cmp = compare_dbo(engine, excel, abs_tol=1.0, rel_tol=0.0)
    assert cmp.summary["within_rate"] == 1.0


# ---------------------------------------------------------------------------
# 2) convention 탐색: 엑셀을 특정 config로 만들면 그 조합을 추천해야 한다
# ---------------------------------------------------------------------------


def _census():
    val = date(2025, 12, 31)
    emps = []
    for k in range(20):
        age = 30 + k
        emps.append(
            Employee(
                emp_id=str(1000 + k),
                birth_date=date(val.year - age, 6, 1),
                gender="M" if k % 2 else "F",
                hire_date=date(val.year - min(age - 22, 20), 3, 1),
                base_salary=3_000_000 + k * 10_000,
                current_year_accrual=10_000_000,
                emp_class="REGULAR",
                plan_type=1,
            )
        )
    return emps


def test_sweep_recommends_the_config_that_generated_excel():
    base = Config.from_yaml("config/assumptions_sample.yaml")
    tables = DecrementTables.from_config(base, base_dir="config")
    records = _census()

    # "기존 엑셀"을 특정 convention 조합으로 생성 (정답).
    # salary_increase_timing·discount_timing은 DBO에 실제로 영향을 주는 식별 가능한 차원.
    truth_cfg = base.model_copy(
        update={"salary_increase_timing": "mid_year", "discount_timing": "mid_year"}
    )
    truth_result = calculate_census(records, truth_cfg, tables, with_detail=False)
    excel_df = result_to_dbo_table(truth_result)

    grid = {
        "salary_increase_timing": ["start_of_year", "mid_year", "end_of_year"],
        "discount_timing": ["end_of_year", "mid_year"],
    }
    sweep = sweep_conventions(records, base, tables, excel_df, grid, abs_tol=1.0, rel_tol=0.0)

    # 추천 조합이 정답과 일치하고 일치율 100%
    assert sweep.best["salary_increase_timing"] == "mid_year"
    assert sweep.best["discount_timing"] == "mid_year"
    assert sweep.best["일치율(%)"] == pytest.approx(100.0)
    assert len(sweep.table) == 6   # 3 × 2 조합


def test_decrement_timing_does_not_affect_dbo():
    # 문서화된 성질: 이 PUC 공식에서 decrement_timing은 DBO에 영향을 주지 않는다
    # (탈퇴시점 총근속 S(t)가 재직비율과 상쇄되고, 할인기간은 discount_timing이 별도 제어).
    base = Config.from_yaml("config/assumptions_sample.yaml")
    tables = DecrementTables.from_config(base, base_dir="config")
    records = _census()
    end = calculate_census(records, base.model_copy(update={"decrement_timing": "end_of_year"}), tables)
    mid = calculate_census(records, base.model_copy(update={"decrement_timing": "mid_year"}), tables)
    assert end.total_dbo == mid.total_dbo


def test_sweep_requires_known_dimension():
    base = Config.from_yaml("config/assumptions_sample.yaml")
    tables = DecrementTables.from_config(base, base_dir="config")
    excel_df = pd.DataFrame({"emp_id": ["1000"], "dbo": [1.0]})
    with pytest.raises(ValueError):
        sweep_conventions(_census(), base, tables, excel_df, grid={"unknown_dim": [1, 2]})
