"""엑셀 대사(對査) 도구.

목적: 기존에 엑셀로 계산한 결과와 엔진 결과를 개인별로 비교해 차이를 찾고,
어떤 설정(convention) 조합이 기존 엑셀과 가장 일치하는지 탐색한다.

1. 비교(compare_dbo): 사번 조인 → 개인별 차이(절대·비율), 요약, 상위 차이, 편측 사번.
2. convention 탐색(sweep_conventions): 설정 그리드를 돌려 조합별 총액차이·일치율 표 + 추천.
3. 개인 추적(track_employee): 차이 큰 사번의 debug 상세를 덤프.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd
import yaml

from .config import Config
from .decrement import DecrementTables
from .engine import CensusResult, calculate_census, dump_employee_detail
from .models import Employee

DEFAULT_ABS_TOL = 1.0        # 1원
DEFAULT_REL_TOL = 0.0001     # 0.01%

# 탐색 대상 convention 차원(그리드 키)
SWEEP_DIMENSIONS = [
    "decrement_timing",
    "salary_increase_timing",
    "discount_timing",
    "service_day_count",
    "retirement_rate_basis",
    "csc_method",
]


# ---------------------------------------------------------------------------
# 결과 파일 로딩 (사번·DBO 컬럼 매핑)
# ---------------------------------------------------------------------------


def load_excel_map(path: Union[str, Path]) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _emp_id_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def load_dbo_table(
    path: Union[str, Path],
    emp_id_column: str = "사번",
    dbo_column: str = "DBO",
    sheet: Optional[str] = None,
) -> pd.DataFrame:
    """결과 파일에서 (emp_id, dbo) 표를 로드해 정규화한다."""
    path = Path(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        raw = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
    else:
        raw = pd.read_csv(path)
    if emp_id_column not in raw.columns or dbo_column not in raw.columns:
        raise ValueError(
            f"컬럼을 찾을 수 없습니다: emp_id='{emp_id_column}', dbo='{dbo_column}' "
            f"(가용 컬럼: {list(raw.columns)})"
        )
    out = pd.DataFrame(
        {
            "emp_id": raw[emp_id_column].map(_emp_id_str),
            "dbo": pd.to_numeric(raw[dbo_column], errors="coerce"),
        }
    )
    return out.dropna(subset=["dbo"]).reset_index(drop=True)


def result_to_dbo_table(result: CensusResult) -> pd.DataFrame:
    """엔진 CensusResult → (emp_id, dbo) 정규화 표."""
    df = result.to_dataframe()
    return pd.DataFrame({"emp_id": df["emp_id"].map(_emp_id_str), "dbo": df["DBO"].astype(float)})


# ---------------------------------------------------------------------------
# 1) 개인별 비교
# ---------------------------------------------------------------------------


@dataclass
class ComparisonResult:
    detail: pd.DataFrame                 # 개인별 차이표
    summary: Dict[str, float]
    top_diff: pd.DataFrame               # 차이 상위 N명
    only_in_engine: List[str]
    only_in_excel: List[str]

    def print_report(self) -> None:
        s = self.summary
        print("── 대사 요약 ──")
        print(f"  공통 사번: {int(s['n_common'])}명 "
              f"(엔진 {int(s['n_engine'])} / 엑셀 {int(s['n_excel'])})")
        print(f"  총 DBO  엔진={s['total_engine']:,.0f}  엑셀={s['total_excel']:,.0f}")
        print(f"  총액 차이={s['total_diff']:,.0f} ({s['total_diff_pct']:.4f}%)")
        print(f"  허용오차 이내: {int(s['within_count'])}/{int(s['n_common'])}명 "
              f"({s['within_rate']*100:.2f}%)")
        if self.only_in_engine:
            print(f"  엔진에만 존재: {len(self.only_in_engine)}명 {self.only_in_engine[:10]}")
        if self.only_in_excel:
            print(f"  엑셀에만 존재: {len(self.only_in_excel)}명 {self.only_in_excel[:10]}")
        if not self.top_diff.empty:
            print("── 차이 상위 ──")
            print(self.top_diff.to_string(index=False))


def compare_dbo(
    engine_df: pd.DataFrame,
    excel_df: pd.DataFrame,
    abs_tol: float = DEFAULT_ABS_TOL,
    rel_tol: float = DEFAULT_REL_TOL,
    top_n: int = 20,
) -> ComparisonResult:
    """엔진 vs 엑셀 (emp_id, dbo) 표를 사번으로 조인해 비교한다.

    허용오차: |차이| <= abs_tol  또는  |차이율| <= rel_tol 이면 일치로 간주.
    """
    eng = engine_df.rename(columns={"dbo": "engine_DBO"})
    exc = excel_df.rename(columns={"dbo": "excel_DBO"})

    merged = eng.merge(exc, on="emp_id", how="outer", indicator=True)
    only_in_engine = merged.loc[merged["_merge"] == "left_only", "emp_id"].tolist()
    only_in_excel = merged.loc[merged["_merge"] == "right_only", "emp_id"].tolist()

    common = merged[merged["_merge"] == "both"].copy()
    common["차이"] = common["engine_DBO"] - common["excel_DBO"]
    # 차이율(%) = 차이 / 엑셀값 × 100. 엑셀값 0(분모 0)이면 0으로.
    denom = common["excel_DBO"].to_numpy(dtype=float)
    diff = common["차이"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(denom != 0, diff / denom * 100.0, 0.0)
    common["차이율(%)"] = ratio
    within = (common["차이"].abs() <= abs_tol) | (common["차이율(%)"].abs() <= rel_tol * 100)
    common["허용오차이내"] = within

    detail = common[["emp_id", "engine_DBO", "excel_DBO", "차이", "차이율(%)", "허용오차이내"]]
    detail = detail.sort_values("차이", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)

    n_common = len(common)
    total_engine = float(common["engine_DBO"].sum())
    total_excel = float(common["excel_DBO"].sum())
    total_diff = total_engine - total_excel
    summary = {
        "n_engine": float(len(engine_df)),
        "n_excel": float(len(excel_df)),
        "n_common": float(n_common),
        "total_engine": total_engine,
        "total_excel": total_excel,
        "total_diff": total_diff,
        "total_diff_pct": (total_diff / total_excel * 100) if total_excel else 0.0,
        "within_count": float(int(within.sum())),
        "within_rate": (float(within.sum()) / n_common) if n_common else 0.0,
    }
    top_diff = detail.head(top_n)
    return ComparisonResult(detail, summary, top_diff, only_in_engine, only_in_excel)


# ---------------------------------------------------------------------------
# 2) convention 그리드 탐색
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    table: pd.DataFrame                  # 조합별 결과 (일치율·총액차이)
    best: Dict                           # 추천 조합
    dimensions: List[str] = field(default_factory=list)

    def print_report(self, top: int = 10) -> None:
        print(f"── convention 탐색: {len(self.table)}개 조합 ──")
        print(self.table.head(top).to_string(index=False))
        print("\n── 추천 조합 (일치율 최고, 동률 시 총액차이 최소) ──")
        for k, v in self.best.items():
            print(f"  {k}: {v}")


def _config_with_overrides(base: Config, overrides: Dict) -> Config:
    data = base.model_dump()
    data.update(overrides)
    return Config.model_validate(data)


def sweep_conventions(
    records: List[Employee],
    base_config: Config,
    tables: DecrementTables,
    excel_df: pd.DataFrame,
    grid: Dict[str, List],
    abs_tol: float = DEFAULT_ABS_TOL,
    rel_tol: float = DEFAULT_REL_TOL,
) -> SweepResult:
    """grid(차원별 후보 리스트)의 모든 조합을 계산해 엑셀과의 일치율을 표로.

    가장 일치율 높은 조합(동률 시 |총액차이| 최소)을 best로 추천한다.
    """
    dims = [d for d in SWEEP_DIMENSIONS if d in grid and grid[d]]
    if not dims:
        raise ValueError(f"탐색할 convention 차원이 없습니다. (지원: {SWEEP_DIMENSIONS})")

    combos = list(itertools.product(*[grid[d] for d in dims]))
    rows = []
    for combo in combos:
        overrides = dict(zip(dims, combo))
        cfg = _config_with_overrides(base_config, overrides)
        result = calculate_census(records, cfg, tables, with_detail=False)
        cmp = compare_dbo(result_to_dbo_table(result), excel_df, abs_tol, rel_tol)
        row = dict(overrides)
        row["일치율(%)"] = round(cmp.summary["within_rate"] * 100, 2)
        row["총 DBO"] = round(cmp.summary["total_engine"], 0)
        row["총액차이"] = round(cmp.summary["total_diff"], 0)
        rows.append(row)

    table = pd.DataFrame(rows)
    table["_abs_diff"] = table["총액차이"].abs()
    table = table.sort_values(["일치율(%)", "_abs_diff"], ascending=[False, True]).reset_index(drop=True)
    best = table.drop(columns="_abs_diff").iloc[0].to_dict()
    table = table.drop(columns="_abs_diff")
    return SweepResult(table=table, best=best, dimensions=dims)


# ---------------------------------------------------------------------------
# 3) 개인 추적
# ---------------------------------------------------------------------------


def track_employee(
    emp_id: str,
    records: List[Employee],
    config: Config,
    tables: DecrementTables,
    out_path: Union[str, Path],
    excel_df: Optional[pd.DataFrame] = None,
) -> Optional[str]:
    """차이가 큰 사번의 debug 상세 테이블을 덤프하고, 엑셀 값과 비교 출력."""
    emp = next((e for e in records if e.emp_id == str(emp_id)), None)
    if emp is None:
        print(f"사번 {emp_id}를 명부에서 찾지 못했습니다.")
        return None

    path = dump_employee_detail(emp, config, tables, str(out_path))
    from .engine import calculate_employee

    res = calculate_employee(emp, config, tables)
    print(f"사번 {emp_id}: 엔진 DBO = {res.dbo:,.0f}")
    if excel_df is not None:
        match = excel_df.loc[excel_df["emp_id"] == str(emp_id), "dbo"]
        if not match.empty:
            excel_val = float(match.iloc[0])
            print(f"           엑셀 DBO = {excel_val:,.0f}  차이 = {res.dbo - excel_val:,.0f}")
    print(f"연도별 상세: {path if path else '(제도구분 2/3은 상세 없음)'}")
    return path


# ---------------------------------------------------------------------------
# 저장 헬퍼
# ---------------------------------------------------------------------------


def write_comparison_excel(out_path: Union[str, Path], comparison: ComparisonResult) -> Path:
    out_path = Path(out_path)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        comparison.detail.to_excel(writer, sheet_name="개인별차이", index=False)
        pd.DataFrame([comparison.summary]).T.rename(columns={0: "값"}).to_excel(
            writer, sheet_name="요약"
        )
        comparison.top_diff.to_excel(writer, sheet_name="차이상위", index=False)
        if comparison.only_in_engine:
            pd.DataFrame({"엔진에만": comparison.only_in_engine}).to_excel(
                writer, sheet_name="편측사번", index=False, startcol=0
            )
        if comparison.only_in_excel:
            pd.DataFrame({"엑셀에만": comparison.only_in_excel}).to_excel(
                writer, sheet_name="편측사번", index=False, startcol=2
            )
    return out_path
