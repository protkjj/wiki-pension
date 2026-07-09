"""산출표·요약 생성.

산출물(엑셀 xlsx):
  1. 개인별 산출표
  2. 요약 보고서 (총 DBO/CSC, 인원, 구분별 소계, 적용 가정 전체)
  3. 민감도 분석 (할인율 ±0.5%p, 임금상승률 ±0.5%p)
  4. 만기분석 (가중평균만기 + 연도별 기대급여지급 현금흐름)
그리고 실행 로그 JSON (입력 SHA-256, config 스냅샷, 엔진버전, 실행시각, 총 결과값).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

# 재현성: 문서 메타데이터에 박히는 생성/수정 시각을 이 고정값으로 치환.
_FIXED_XLSX_DT = b"2000-01-01T00:00:00Z"
_CORE_DT_RE = re.compile(
    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)

from . import __version__
from .config import Config
from .decrement import DecrementTables
from .engine import (
    CensusResult,
    calculate_census,
    expected_cashflows,
    weighted_average_duration,
)
from .models import Employee

_PLAN_LABEL = {1: "1_DB정상평가", 2: "2_간편법", 3: "3_제외"}


# ---------------------------------------------------------------------------
# 1) 개인별 산출표
# ---------------------------------------------------------------------------


def build_individual_table(
    census_result: CensusResult,
    records: List[Employee],
    report=None,
) -> pd.DataFrame:
    """사번·인적정보·근속·급여·DBO·CSC·제도구분·검증플래그 개인별 표."""
    by_id = {e.emp_id: e for e in records}
    flagged = report.flagged_emp_ids() if report is not None else set()

    rows = []
    for r in census_result.results:
        emp = by_id.get(r.emp_id)
        rows.append(
            {
                "사번": r.emp_id,
                "생년월일": emp.birth_date if emp else None,
                "성별": (emp.gender.value if emp else None),
                "입사일": emp.hire_date if emp else None,
                "근속연수": round(r.attained_service, 4),
                "기준급여": r.base_salary,
                "당년도추계액": r.current_year_accrual,
                "DBO": r.dbo,
                "CSC": r.csc,
                "제도구분": _PLAN_LABEL.get(r.plan_type, r.plan_type),
                "검증플래그": "경고" if r.emp_id in flagged else "",
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2) 요약 (가정 + 총계 + 소계)
# ---------------------------------------------------------------------------


def build_summary_blocks(census_result: CensusResult, config: Config) -> dict:
    """요약 시트에 쓸 블록들(가정/총계/구분별/제도별)을 DataFrame으로 반환."""
    assumptions = pd.DataFrame(
        [
            ("산출기준일", str(config.valuation_date)),
            ("할인율", config.discount_rate.flat),
            ("임금상승률", config.salary_increase_rate.flat),
            ("정년(기본)", config.retirement_age.default),
            ("정년(구분별)", str({k.value: v for k, v in config.retirement_age.by_class.items()})),
            ("decrement_timing", config.decrement_timing),
            ("salary_increase_timing", config.salary_increase_timing),
            ("discount_timing", config.discount_timing),
            ("service_day_count", config.service_day_count),
            ("retirement_rate_basis", config.retirement_rate_basis),
            ("csc_method", config.csc_method),
            ("rounding", config.rounding),
        ],
        columns=["가정항목", "값"],
    )

    totals = pd.DataFrame(
        [
            ("총 DBO", census_result.total_dbo),
            ("총 CSC", census_result.total_csc),
            ("계산대상 인원", len(census_result.results)),
            ("제외(제도3) 인원", len(census_result.excluded_emp_ids)),
        ],
        columns=["항목", "값"],
    )

    by_class = pd.DataFrame(
        [
            {"종업원구분": k, "인원": v["count"], "DBO": v["DBO"], "CSC": v["CSC"]}
            for k, v in census_result.subtotal_by_class.items()
        ]
    )
    by_plan = pd.DataFrame(
        [
            {"제도구분": _PLAN_LABEL.get(k, k), "인원": v["count"], "DBO": v["DBO"], "CSC": v["CSC"]}
            for k, v in census_result.subtotal_by_plan.items()
        ]
    )
    return {"assumptions": assumptions, "totals": totals, "by_class": by_class, "by_plan": by_plan}


# ---------------------------------------------------------------------------
# 3) 민감도 분석
# ---------------------------------------------------------------------------


def _reprice(records, config: Config, tables, discount=None, salary=None) -> CensusResult:
    data = config.model_dump()
    if discount is not None:
        data["discount_rate"] = {"flat": discount}
    if salary is not None:
        data["salary_increase_rate"] = {"flat": salary}
    new_cfg = Config.model_validate(data)
    return calculate_census(records, new_cfg, tables, with_detail=False)


def build_sensitivity(records: List[Employee], config: Config, tables: DecrementTables) -> pd.DataFrame:
    """할인율 ±0.5%p, 임금상승률 ±0.5%p 각각 재계산한 DBO 표."""
    base = calculate_census(records, config, tables, with_detail=False)
    base_dbo = base.total_dbo
    d = config.discount_rate.flat
    s = config.salary_increase_rate.flat

    scenarios = [
        ("기준", base_dbo),
        ("할인율 +0.5%p", _reprice(records, config, tables, discount=d + 0.005).total_dbo),
        ("할인율 -0.5%p", _reprice(records, config, tables, discount=d - 0.005).total_dbo),
        ("임금상승률 +0.5%p", _reprice(records, config, tables, salary=s + 0.005).total_dbo),
        ("임금상승률 -0.5%p", _reprice(records, config, tables, salary=s - 0.005).total_dbo),
    ]
    rows = []
    for name, dbo in scenarios:
        rows.append(
            {
                "시나리오": name,
                "총 DBO": dbo,
                "변화액": dbo - base_dbo,
                "변화율(%)": (dbo / base_dbo - 1.0) * 100 if base_dbo else 0.0,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 4) 만기분석 (듀레이션 + 현금흐름)
# ---------------------------------------------------------------------------


def build_maturity(records: List[Employee], config: Config, tables: DecrementTables):
    """(가중평균만기, 현금흐름표) 반환. 향후 10년 개별 + 이후 5년 구간별."""
    cf = expected_cashflows(records, config, tables)
    duration = weighted_average_duration(cf)

    if cf.empty:
        return duration, pd.DataFrame(columns=["구간", "기대급여지급액", "현재가치"])

    rows = []
    for _, r in cf[cf["연도"] <= 10].iterrows():
        rows.append({"구간": f"{int(r['연도'])}년", "기대급여지급액": r["기대급여지급액"], "현재가치": r["현재가치"]})

    rest = cf[cf["연도"] > 10]
    if not rest.empty:
        max_year = int(rest["연도"].max())
        start = 11
        while start <= max_year:
            end = start + 4
            band = rest[(rest["연도"] >= start) & (rest["연도"] <= end)]
            if not band.empty:
                rows.append(
                    {
                        "구간": f"{start}~{end}년",
                        "기대급여지급액": band["기대급여지급액"].sum(),
                        "현재가치": band["현재가치"].sum(),
                    }
                )
            start = end + 1
    return duration, pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 엑셀 저장
# ---------------------------------------------------------------------------


def _normalize_xlsx(path: Path) -> None:
    """xlsx(zip) 내부 엔트리의 타임스탬프를 고정해 바이트 재현성을 보장한다.

    openpyxl은 각 zip 엔트리에 기록 시점의 벽시계 시각을 넣어, 초 단위가 달라지면
    같은 내용이라도 파일 바이트가 달라진다. 모든 엔트리를 고정 시각으로 다시 써서 제거.
    """
    path = Path(path)
    fixed = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(path) as zin:
        entries = [(info, zin.read(info.filename)) for info in zin.infolist()]

    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, content in entries:
            # core.xml의 생성/수정 시각(openpyxl이 저장 시각으로 덮어씀)을 고정값으로.
            if info.filename == "docProps/core.xml":
                content = _CORE_DT_RE.sub(rb"\g<1>" + _FIXED_XLSX_DT + rb"\g<2>", content)
            zi = zipfile.ZipInfo(info.filename, date_time=fixed)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zout.writestr(zi, content)
    os.replace(tmp, path)


def write_excel(
    out_path: Path,
    individual: pd.DataFrame,
    summary_blocks: dict,
    sensitivity: pd.DataFrame,
    duration: float,
    maturity: pd.DataFrame,
    excluded_emp_ids: List[str],
) -> Path:
    """모든 시트를 하나의 xlsx로 저장."""
    out_path = Path(out_path)
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        # (문서 메타 생성/수정시각은 저장 후 _normalize_xlsx가 고정값으로 치환)
        individual.to_excel(writer, sheet_name="개인별산출표", index=False)

        # 요약: 여러 블록을 startrow로 쌓아 한 시트에
        row = 0
        for key, title in [
            ("totals", "■ 총계"),
            ("assumptions", "■ 적용 가정"),
            ("by_class", "■ 종업원구분별 소계"),
            ("by_plan", "■ 제도구분별 소계"),
        ]:
            block = summary_blocks[key]
            pd.DataFrame({title: []}).to_excel(writer, sheet_name="요약", startrow=row, index=False)
            row += 1
            block.to_excel(writer, sheet_name="요약", startrow=row, index=False)
            row += len(block) + 3

        sensitivity.to_excel(writer, sheet_name="민감도분석", index=False)

        mat_header = pd.DataFrame([{"항목": "가중평균만기(년)", "값": round(duration, 4)}])
        mat_header.to_excel(writer, sheet_name="만기분석", startrow=0, index=False)
        maturity.to_excel(writer, sheet_name="만기분석", startrow=3, index=False)

        if excluded_emp_ids:
            pd.DataFrame({"제외사번(제도3)": excluded_emp_ids}).to_excel(
                writer, sheet_name="제외목록", index=False
            )
    _normalize_xlsx(out_path)   # 재현성: zip 타임스탬프 고정
    return out_path


# ---------------------------------------------------------------------------
# 5) 실행 로그 JSON
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_run_log(
    out_path: Path,
    census_path: Path,
    config: Config,
    census_result: CensusResult,
    timestamp: Optional[str] = None,
) -> Path:
    """실행 로그 JSON: 입력 해시, config 스냅샷, 엔진버전, 실행시각, 총 결과값."""
    out_path = Path(out_path)
    log = {
        "engine_version": __version__,
        "run_timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "input": {
            "census_path": str(census_path),
            "census_sha256": file_sha256(census_path),
        },
        "config_snapshot": json.loads(config.model_dump_json()),
        "results": {
            "total_dbo": census_result.total_dbo,
            "total_csc": census_result.total_csc,
            "n_calculated": len(census_result.results),
            "n_excluded": len(census_result.excluded_emp_ids),
        },
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=2, default=str)
    return out_path


# ---------------------------------------------------------------------------
# 오케스트레이터
# ---------------------------------------------------------------------------


def write_outputs(
    out_dir,
    records: List[Employee],
    census_result: CensusResult,
    config: Config,
    tables: DecrementTables,
    census_path,
    report=None,
    timestamp: Optional[str] = None,
    company: str = "회사",
    plan_info: Optional[dict] = None,
    prior: Optional[dict] = None,
    disclosure_inputs: Optional[dict] = None,
) -> dict:
    """전체 산출물(상세 xlsx + 전문 보고서 xlsx + run_log.json)을 저장."""
    from .report import write_report  # 순환 import 방지용 지연 import

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    individual = build_individual_table(census_result, records, report)
    summary_blocks = build_summary_blocks(census_result, config)
    sensitivity = build_sensitivity(records, config, tables)
    duration, maturity = build_maturity(records, config, tables)

    xlsx_path = write_excel(
        out_dir / "dbo_results.xlsx",
        individual, summary_blocks, sensitivity, duration, maturity,
        census_result.excluded_emp_ids,
    )
    report_path = write_report(
        out_dir / "dbo_report.xlsx", company, config.valuation_date, config,
        census_result, records, tables,
        disclosure_inputs=disclosure_inputs, plan_info=plan_info, prior=prior,
    )
    _normalize_xlsx(report_path)   # 재현성: zip/메타 타임스탬프 고정

    # 파워포인트 발표자료 보고서 (python-pptx 미설치 등으로 실패해도 전체 계산은 진행)
    pptx_path = None
    try:
        from .report_ppt import write_report_pptx
        pptx_path = write_report_pptx(
            out_dir / "dbo_report.pptx", company, config.valuation_date, config,
            census_result, records, tables,
            disclosure_inputs=disclosure_inputs, plan_info=plan_info, prior=prior,
        )
        _normalize_xlsx(pptx_path)     # 재현성: pptx(zip)도 타임스탬프 고정
    except Exception:  # noqa: BLE001  (예: python-pptx 미설치)
        pptx_path = None

    log_path = write_run_log(out_dir / "run_log.json", Path(census_path), config, census_result, timestamp)
    return {"xlsx": xlsx_path, "report": report_path, "report_pptx": pptx_path, "run_log": log_path}
