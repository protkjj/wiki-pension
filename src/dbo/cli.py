"""CLI 진입점.

`validate`(명부 검증)와 `run`(전체 계산·산출물 생성)이 동작한다.
`reconcile`은 프롬프트 4에서 구현된다.

사용 예:
  dbo validate --census data/census.xlsx --config config/assumptions.yaml
  dbo run --census data/census.xlsx --config config/assumptions.yaml --out results/
  dbo run ... --debug-emp 12345    # 해당 사번 상세 덤프
  dbo reconcile --engine ... --excel ... --map ...      # 프롬프트 4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .census import load_census, validate_census
from .config import Config
from .decrement import DecrementTables
from .engine import calculate_census, dump_employee_detail
from .outputs import write_outputs


def _cmd_validate(args: argparse.Namespace) -> int:
    config = Config.from_yaml(args.config) if args.config else None
    if config is None:
        print("경고: --config 미지정. 도메인 검증(기준일 대비)은 건너뜁니다.", file=sys.stderr)

    records, report, _ = load_census(args.census, column_map=args.map_)
    if config is not None:
        validate_census(records, config.valuation_date, report)

    print(report.summary())
    for issue in report.errors:
        print(f"[오류] {issue.emp_id or '-'} {issue.rule}: {issue.message}")
    for issue in report.warnings:
        print(f"[경고] {issue.emp_id or '-'} {issue.rule}: {issue.message}")

    return 1 if report.has_errors else 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_yaml(args.config)
    config_dir = Path(args.config).resolve().parent
    tables = DecrementTables.from_config(config, base_dir=config_dir)

    records, report, _ = load_census(args.census, column_map=args.map_)
    validate_census(records, config.valuation_date, report)

    if report.has_errors:
        print(f"검증 오류 {len(report.errors)}건으로 계산을 중단합니다.", file=sys.stderr)
        for issue in report.errors:
            print(f"[오류] {issue.emp_id or '-'} {issue.rule}: {issue.message}", file=sys.stderr)
        return 1
    if report.warnings:
        print(f"경고 {len(report.warnings)}건 (플래그 후 계속 진행).", file=sys.stderr)

    result = calculate_census(records, config, tables, with_detail=False)

    args.out.mkdir(parents=True, exist_ok=True)

    # debug: 지정 사번 상세 덤프
    if args.debug_emp:
        target = next((e for e in records if e.emp_id == str(args.debug_emp)), None)
        if target is None:
            print(f"경고: --debug-emp {args.debug_emp} 사번을 명부에서 찾지 못했습니다.", file=sys.stderr)
        else:
            path = dump_employee_detail(target, config, tables, str(args.out / f"detail_{args.debug_emp}.csv"))
            print(f"상세 덤프: {path if path else '(제도구분 2/3은 상세 없음)'}")

    paths = write_outputs(
        args.out, records, result, config, tables,
        census_path=args.census, report=report,
    )
    print(f"총 DBO = {result.total_dbo:,.0f}  총 CSC = {result.total_csc:,.0f}")
    print(f"계산대상 {len(result.results)}명, 제외 {len(result.excluded_emp_ids)}명")
    print(f"산출물: {paths['xlsx']}")
    print(f"실행로그: {paths['run_log']}")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from .reconcile import (
        compare_dbo,
        load_dbo_table,
        load_excel_map,
        track_employee,
        write_comparison_excel,
    )

    emap = load_excel_map(args.map_) if args.map_ else {}
    engine_df = load_dbo_table(
        args.engine,
        emp_id_column=emap.get("engine_emp_id_column", "사번"),
        dbo_column=emap.get("engine_dbo_column", "DBO"),
        sheet=emap.get("engine_sheet", "개인별산출표"),
    )
    excel_df = load_dbo_table(
        args.excel,
        emp_id_column=emap.get("emp_id_column", "사번"),
        dbo_column=emap.get("dbo_column", "DBO"),
        sheet=emap.get("sheet"),
    )

    cmp = compare_dbo(engine_df, excel_df, abs_tol=args.abs_tol, rel_tol=args.rel_tol)
    cmp.print_report()

    if args.out:
        path = write_comparison_excel(args.out, cmp)
        print(f"대사 결과: {path}")

    # 개인 추적 (선택): --track 사번 + --census + --config 필요
    if args.track:
        if not (args.census and args.config):
            print("경고: --track 사용에는 --census, --config가 필요합니다.", file=sys.stderr)
        else:
            config = Config.from_yaml(args.config)
            tables = DecrementTables.from_config(config, base_dir=Path(args.config).resolve().parent)
            records, _, _ = load_census(args.census, column_map=args.census_map)
            out_csv = (args.out.parent if args.out else Path(".")) / f"track_{args.track}.csv"
            track_employee(str(args.track), records, config, tables, out_csv, excel_df=excel_df)
    return 0


def _cmd_reconcile_sweep(args: argparse.Namespace) -> int:
    import yaml

    from .reconcile import load_dbo_table, load_excel_map, sweep_conventions

    config = Config.from_yaml(args.config)
    tables = DecrementTables.from_config(config, base_dir=Path(args.config).resolve().parent)
    records, report, _ = load_census(args.census, column_map=args.census_map)
    validate_census(records, config.valuation_date, report)
    if report.has_errors:
        print(f"검증 오류 {len(report.errors)}건으로 탐색을 중단합니다.", file=sys.stderr)
        return 1

    emap = load_excel_map(args.map_) if args.map_ else {}
    excel_df = load_dbo_table(
        args.excel,
        emp_id_column=emap.get("emp_id_column", "사번"),
        dbo_column=emap.get("dbo_column", "DBO"),
        sheet=emap.get("sheet"),
    )

    with Path(args.grid).open("r", encoding="utf-8") as fh:
        grid = yaml.safe_load(fh) or {}

    sweep = sweep_conventions(
        records, config, tables, excel_df, grid,
        abs_tol=args.abs_tol, rel_tol=args.rel_tol,
    )
    sweep.print_report()

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        sweep.table.to_excel(args.out, index=False)
        print(f"탐색 결과: {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbo", description="퇴직급여부채(DBO) 계산 엔진")
    parser.add_argument("--version", action="version", version=f"dbo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="명부 검증만 수행")
    p_val.add_argument("--census", required=True, type=Path, help="명부 파일 (xlsx/csv)")
    p_val.add_argument("--config", type=Path, help="설정 YAML (기준일 검증에 필요)")
    p_val.add_argument("--map", dest="map_", type=Path, help="컬럼 매핑 YAML (한글 컬럼명)")
    p_val.set_defaults(func=_cmd_validate)

    p_run = sub.add_parser("run", help="전체 계산 실행 및 산출물 생성")
    p_run.add_argument("--census", required=True, type=Path)
    p_run.add_argument("--config", required=True, type=Path)
    p_run.add_argument("--out", required=True, type=Path)
    p_run.add_argument("--map", dest="map_", type=Path, help="컬럼 매핑 YAML (한글 컬럼명)")
    p_run.add_argument("--debug-emp", dest="debug_emp", help="해당 사번 상세 덤프")
    p_run.set_defaults(func=_cmd_run)

    p_rec = sub.add_parser("reconcile", help="엔진 결과 vs 기존 엑셀 개인별 대사")
    p_rec.add_argument("--engine", required=True, type=Path, help="엔진 결과 파일 (xlsx)")
    p_rec.add_argument("--excel", required=True, type=Path, help="기존 엑셀 결과 파일")
    p_rec.add_argument("--map", dest="map_", type=Path, help="사번·DBO 컬럼 매핑 YAML")
    p_rec.add_argument("--out", type=Path, help="대사 결과 xlsx 저장 경로")
    p_rec.add_argument("--abs-tol", dest="abs_tol", type=float, default=1.0, help="절대 허용오차(원)")
    p_rec.add_argument("--rel-tol", dest="rel_tol", type=float, default=0.0001, help="상대 허용오차")
    p_rec.add_argument("--track", help="개인 추적: 상세 덤프할 사번")
    p_rec.add_argument("--census", type=Path, help="--track 시 명부 파일")
    p_rec.add_argument("--config", type=Path, help="--track 시 설정 YAML")
    p_rec.add_argument("--census-map", dest="census_map", type=Path, help="명부 컬럼 매핑 YAML")
    p_rec.set_defaults(func=_cmd_reconcile)

    p_sweep = sub.add_parser("reconcile-sweep", help="convention 조합 그리드 탐색")
    p_sweep.add_argument("--census", required=True, type=Path)
    p_sweep.add_argument("--config", required=True, type=Path)
    p_sweep.add_argument("--excel", required=True, type=Path)
    p_sweep.add_argument("--grid", required=True, type=Path, help="탐색 그리드 YAML")
    p_sweep.add_argument("--map", dest="map_", type=Path, help="엑셀 사번·DBO 컬럼 매핑 YAML")
    p_sweep.add_argument("--census-map", dest="census_map", type=Path, help="명부 컬럼 매핑 YAML")
    p_sweep.add_argument("--out", type=Path, help="탐색 결과 xlsx 저장 경로")
    p_sweep.add_argument("--abs-tol", dest="abs_tol", type=float, default=1.0)
    p_sweep.add_argument("--rel-tol", dest="rel_tol", type=float, default=0.0001)
    p_sweep.set_defaults(func=_cmd_reconcile_sweep)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
