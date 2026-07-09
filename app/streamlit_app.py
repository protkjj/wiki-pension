"""DBO 계산 엔진 — Streamlit 웹 앱.

엔진(src/dbo)을 그대로 재사용하는 로컬 실행용 화면.
흐름: ① 명부 업로드·검증 → ② 가정 입력·계산·결과·다운로드 → ③ 엑셀 대사.

실행:
  pip install -e ".[app]"        # 또는: pip install streamlit
  streamlit run app/streamlit_app.py

⚠️ 개인정보 보호: 실제 명부(개인정보)는 이 앱을 **로컬/사내망**에서 실행할 때만
   업로드하세요. 공개 클라우드에 배포된 인스턴스에는 실데이터를 올리지 마세요.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
CONFIG_DIR = ROOT / "config"

from dbo import __version__  # noqa: E402
from dbo import table_store as ts  # noqa: E402
from dbo.census import Severity, load_census, validate_census  # noqa: E402
from dbo.config import Config  # noqa: E402
from dbo.decrement import DecrementTables  # noqa: E402
from dbo.engine import calculate_census  # noqa: E402
from dbo.outputs import build_maturity, build_sensitivity, write_outputs  # noqa: E402
from dbo.reconcile import (  # noqa: E402
    compare_dbo,
    load_dbo_table,
    result_to_dbo_table,
    sweep_conventions,
)

st.set_page_config(page_title="DBO 계산 엔진", page_icon="📊", layout="wide")


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def eok(v: float) -> str:
    return f"{v/1e8:,.1f}억"


def won(v: float) -> str:
    return f"{round(v):,}원"


def _save_upload(uploaded) -> Path:
    """업로드 파일을 임시 경로에 저장하고 경로 반환."""
    suffix = Path(uploaded.name).suffix
    tmp = Path(tempfile.gettempdir()) / f"dbo_upload_{uploaded.name}"
    tmp.write_bytes(uploaded.getbuffer())
    return tmp


def build_config(inputs: dict) -> Config:
    return Config.from_dict(
        {
            "valuation_date": inputs["valuation_date"].isoformat(),
            "discount_rate": inputs["discount_rate"],
            "salary_increase_rate": inputs["salary_increase_rate"],
            "retirement_age": inputs["retirement_age"],
            "decrement_timing": inputs["decrement_timing"],
            "salary_increase_timing": inputs["salary_increase_timing"],
            "discount_timing": inputs["discount_timing"],
            "service_day_count": inputs["service_day_count"],
            "retirement_rate_basis": inputs["retirement_rate_basis"],
            "csc_method": inputs["csc_method"],
            "rounding": inputs["rounding"],
            # 선택한 탈퇴율 버전의 테이블 경로 (run_log에도 이 경로가 기록됨)
            "decrement_tables": ts.relative_paths(inputs.get("dec_version", "기본")),
        }
    )


def _read_upload_table(uploaded):
    """업로드된 테이블 파일(xlsx/csv)을 DataFrame으로 읽는다."""
    if uploaded is None:
        return None
    if uploaded.name.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(uploaded)
    return pd.read_csv(uploaded)


# ---------------------------------------------------------------------------
# 사이드바: 계산 가정
# ---------------------------------------------------------------------------

st.sidebar.title("📊 DBO 계산 엔진")
st.sidebar.caption(f"K-IFRS 1019 PUC · engine v{__version__}")
st.sidebar.header("계산 가정")

inputs = {
    "valuation_date": st.sidebar.date_input("산출기준일", dt.date(2025, 12, 31)),
    "discount_rate": st.sidebar.number_input("할인율 (%)", value=4.5, step=0.1, format="%.2f") / 100,
    "salary_increase_rate": st.sidebar.number_input("임금상승률 (%)", value=3.0, step=0.1, format="%.2f") / 100,
    "retirement_age": st.sidebar.number_input("정년", value=60, step=1),
}
with st.sidebar.expander("계산 convention (고급)", expanded=False):
    inputs["decrement_timing"] = st.selectbox("탈퇴시점", ["end_of_year", "mid_year"],
        help="DBO에는 영향 없음(재직비율과 상쇄). 상세표 표시에만 영향.")
    inputs["salary_increase_timing"] = st.selectbox("임금상승 반영", ["start_of_year", "mid_year", "end_of_year"])
    inputs["discount_timing"] = st.selectbox("할인기간 산정", ["end_of_year", "mid_year"])
    inputs["service_day_count"] = st.selectbox("근속 일할", ["act/365", "act/365.25", "months"])
    inputs["retirement_rate_basis"] = st.selectbox("퇴직률 기준", ["age", "service"])
    inputs["csc_method"] = st.selectbox("CSC 방식", ["one_year_slice", "attained_minus_prior"])
    inputs["rounding"] = st.number_input("반올림 단위 (원)", value=1, step=1)

inputs.setdefault("decrement_timing", "end_of_year")
inputs.setdefault("salary_increase_timing", "start_of_year")
inputs.setdefault("discount_timing", "end_of_year")
inputs.setdefault("service_day_count", "act/365")
inputs.setdefault("retirement_rate_basis", "age")
inputs.setdefault("csc_method", "one_year_slice")
inputs.setdefault("rounding", 1)

# 탈퇴율 테이블 버전 선택 (연도 등). ④ 탭에서 업로드·관리.
st.sidebar.header("탈퇴율 테이블")
_versions = ["기본"] + ts.list_versions(str(CONFIG_DIR))
inputs["dec_version"] = st.sidebar.selectbox(
    "버전", _versions,
    help="퇴직·사망률 테이블 버전. '④ 적용 가정·탈퇴율' 탭에서 엑셀 업로드로 새 버전 등록.",
)

config = build_config(inputs)
tables = DecrementTables.from_config(config, base_dir=str(CONFIG_DIR))

st.sidebar.info("⚠️ 실데이터(개인정보)는 로컬/사내망 실행 시에만 업로드하세요.")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

st.title("퇴직급여부채(DBO) 계산")
tab1, tab2, tab3, tab4 = st.tabs(
    ["① 명부·검증", "② 계산 결과", "③ 엑셀 대사", "④ 적용 가정·탈퇴율"]
)


# --- 탭1: 명부 업로드·검증 -------------------------------------------------
with tab1:
    st.subheader("명부 업로드 및 검증")
    c1, c2 = st.columns([2, 1])
    with c1:
        uploaded = st.file_uploader("명부 파일 (xlsx / csv)", type=["xlsx", "csv"])
    with c2:
        map_mode = st.radio("컬럼 매핑", ["한글 샘플 매핑", "표준 스키마", "YAML 업로드"])

    column_map = None
    if map_mode == "한글 샘플 매핑":
        column_map = str(CONFIG_DIR / "column_map_sample.yaml")
    elif map_mode == "YAML 업로드":
        map_file = st.file_uploader("컬럼 매핑 YAML", type=["yaml", "yml"], key="mapyaml")
        if map_file:
            column_map = yaml.safe_load(map_file.getvalue())

    st.caption("표본 명부가 없으면: `python scripts/generate_sample_census.py --out data/sample_census.xlsx`")

    if uploaded is not None:
        path = _save_upload(uploaded)
        records, report, df = load_census(path, column_map=column_map)
        validate_census(records, config.valuation_date, report)
        from dbo.smart_checks import run_smart_checks
        from dbo.actuary_checks import run_actuary_checks
        run_smart_checks(records, config.valuation_date, report)
        run_actuary_checks(records, config.valuation_date, report, config=config)

        m1, m2, m3 = st.columns(3)
        m1.metric("레코드", f"{report.n_records}건")
        m2.metric("오류", f"{len(report.errors)}건", delta=None,
                  delta_color="inverse" if report.errors else "off")
        m3.metric("경고", f"{len(report.warnings)}건")

        if report.errors:
            st.error(f"오류 {len(report.errors)}건 — 계산이 중단됩니다. 수정 후 다시 업로드하세요.")
            st.dataframe(pd.DataFrame([
                {"사번": i.emp_id, "규칙": i.rule, "내용": i.message} for i in report.errors
            ]), width='stretch', hide_index=True)
        else:
            st.success("치명 오류 없음 — ② 계산 결과 탭으로 진행하세요.")
        if report.warnings:
            with st.expander(f"경고 {len(report.warnings)}건 (플래그 후 진행)"):
                st.dataframe(pd.DataFrame([
                    {"사번": i.emp_id, "규칙": i.rule, "내용": i.message} for i in report.warnings
                ]), width='stretch', hide_index=True)

        # --- 문제 행을 명부 레이아웃 그대로 표시 (어디를 고칠지 바로 확인) ---
        if report.issues:
            # 사번 → df 행 인덱스 매핑 (도메인 오류는 사번으로, 파싱 오류는 행번호로 위치)
            id_to_rows: dict = {}
            if "emp_id" in df.columns:
                for idx, v in df["emp_id"].items():
                    key = None if v is None or pd.isna(v) else str(v).strip()
                    id_to_rows.setdefault(key, []).append(idx)

            problems: dict = {}   # df 행 인덱스 → 문제 메시지 목록
            for issue in report.issues:
                tag = "🔴오류" if issue.severity == Severity.ERROR else "🟡경고"
                rows = [issue.row] if issue.row is not None else id_to_rows.get(
                    str(issue.emp_id) if issue.emp_id is not None else None, [])
                for r in rows:
                    problems.setdefault(r, []).append(f"{tag} {issue.message}")

            if problems:
                idxs = sorted(i for i in problems if i in df.index)
                view = df.loc[idxs].copy()
                view.insert(0, "엑셀행", [i + 2 for i in idxs])        # 헤더 1행 + 0-based
                view.insert(1, "⚠문제", [" / ".join(problems[i]) for i in idxs])
                st.markdown("**명부에서 문제가 있는 행** (엑셀행 = 원본 파일의 행 번호)")
                st.dataframe(view, width='stretch', hide_index=True)
                st.caption("이 행들을 원본 엑셀에서 수정한 뒤 다시 업로드하세요.")

        st.session_state["records"] = records
        st.session_state["report"] = report
        st.session_state["census_path"] = str(path)
        st.session_state["has_errors"] = report.has_errors


# --- 탭2: 계산 결과 --------------------------------------------------------
with tab2:
    st.subheader("계산 결과")
    if "records" not in st.session_state:
        st.info("먼저 ① 탭에서 명부를 업로드하세요.")
    elif st.session_state.get("has_errors"):
        st.warning("검증 오류가 있어 계산할 수 없습니다. ① 탭에서 수정 후 다시 업로드하세요.")
    else:
        records = st.session_state["records"]
        report = st.session_state["report"]
        result = calculate_census(records, config, tables, with_detail=False)
        sens = build_sensitivity(records, config, tables)
        duration, maturity = build_maturity(records, config, tables)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("총 DBO", eok(result.total_dbo), help=won(result.total_dbo))
        k2.metric("총 CSC", eok(result.total_csc), help=won(result.total_csc))
        k3.metric("계산대상", f"{len(result.results)}명",
                  help=f"제외(제도3) {len(result.excluded_emp_ids)}명")
        k4.metric("가중평균만기", f"{duration:.1f}년")

        st.divider()
        cc1, cc2 = st.columns(2)
        with cc1:
            st.markdown("**종업원구분별 DBO**")
            cls = pd.DataFrame([
                {"구분": k, "인원": v["count"], "DBO": v["DBO"]}
                for k, v in result.subtotal_by_class.items()
            ]).set_index("구분")
            st.bar_chart(cls["DBO"], color="#2E5A87", horizontal=True)
            st.dataframe(cls, width='stretch')
        with cc2:
            st.markdown("**제도구분별 소계**")
            pln = pd.DataFrame([
                {"제도": {1: "1_DB정상", 2: "2_간편법", 3: "3_제외"}.get(k, k),
                 "인원": v["count"], "DBO": v["DBO"], "CSC": v["CSC"]}
                for k, v in result.subtotal_by_plan.items()
            ]).set_index("제도")
            st.dataframe(pln, width='stretch')

        st.divider()
        sc1, sc2 = st.columns(2)
        with sc1:
            st.markdown("**민감도 분석** (±0.5%p)")
            sens_view = sens[sens["시나리오"] != "기준"][["시나리오", "변화율(%)", "변화액"]]
            st.dataframe(sens_view.set_index("시나리오").style.format(
                {"변화율(%)": "{:+.2f}", "변화액": "{:+,.0f}"}), width='stretch')
        with sc2:
            st.markdown("**만기분석** · 구간별 현금흐름")
            mat_idx = maturity.set_index("구간")[["기대급여지급액", "현재가치"]]
            st.bar_chart(mat_idx)

        st.divider()
        st.markdown("**개인별 산출표**")
        indiv = result.to_dataframe().sort_values("DBO", ascending=False)
        st.dataframe(indiv, width='stretch', hide_index=True)

        # 다운로드
        st.divider()
        st.markdown("**산출물 다운로드**")
        out_dir = Path(tempfile.mkdtemp(prefix="dbo_out_"))
        paths = write_outputs(out_dir, records, result, config, tables,
                              census_path=st.session_state["census_path"], report=report,
                              timestamp="(웹 실행)")
        d1, d2, d3 = st.columns(3)
        d1.download_button("📑 계리평가보고서", Path(paths["report"]).read_bytes(),
                           file_name="dbo_report.xlsx", width='stretch')
        d2.download_button("📄 상세 산출물", Path(paths["xlsx"]).read_bytes(),
                           file_name="dbo_results.xlsx", width='stretch')
        d3.download_button("🧾 실행 로그(json)", Path(paths["run_log"]).read_bytes(),
                           file_name="run_log.json", width='stretch')


# --- 탭3: 엑셀 대사 --------------------------------------------------------
with tab3:
    st.subheader("엑셀 대사 (기존 계산 vs 엔진)")
    if "records" not in st.session_state or st.session_state.get("has_errors"):
        st.info("먼저 ① 탭에서 명부를 업로드하세요.")
    else:
        records = st.session_state["records"]
        excel_up = st.file_uploader("기존 엑셀 결과 파일 (사번·DBO 포함)", type=["xlsx", "csv"], key="excel")
        c1, c2 = st.columns(2)
        emp_col = c1.text_input("사번 컬럼명", value="사번")
        dbo_col = c2.text_input("DBO 컬럼명", value="퇴직급여부채")

        if excel_up is not None:
            xpath = _save_upload(excel_up)
            try:
                excel_df = load_dbo_table(xpath, emp_id_column=emp_col, dbo_column=dbo_col)
            except ValueError as e:
                st.error(str(e))
                excel_df = None

            if excel_df is not None:
                mode = st.radio("작업", ["개인별 비교", "convention 탐색(sweep)"], horizontal=True)
                result = calculate_census(records, config, tables, with_detail=False)
                engine_df = result_to_dbo_table(result)

                if mode == "개인별 비교":
                    cmp = compare_dbo(engine_df, excel_df)
                    s = cmp.summary
                    a, b, c = st.columns(3)
                    a.metric("공통 사번", f"{int(s['n_common'])}명")
                    b.metric("허용오차 이내", f"{s['within_rate']*100:.1f}%")
                    c.metric("총액 차이", won(s["total_diff"]))
                    if cmp.only_in_engine or cmp.only_in_excel:
                        st.caption(f"엔진에만 {len(cmp.only_in_engine)}명 · 엑셀에만 {len(cmp.only_in_excel)}명")
                    st.markdown("**차이 상위**")
                    st.dataframe(cmp.top_diff, width='stretch', hide_index=True)
                else:
                    st.caption("탐색할 convention 차원을 고르면 모든 조합을 계산해 일치율을 비교합니다.")
                    grid = {}
                    if st.checkbox("salary_increase_timing", value=True):
                        grid["salary_increase_timing"] = ["start_of_year", "mid_year", "end_of_year"]
                    if st.checkbox("discount_timing", value=True):
                        grid["discount_timing"] = ["end_of_year", "mid_year"]
                    if st.checkbox("service_day_count", value=True):
                        grid["service_day_count"] = ["act/365", "act/365.25"]
                    if st.checkbox("csc_method"):
                        grid["csc_method"] = ["one_year_slice", "attained_minus_prior"]
                    if st.button("탐색 실행", type="primary") and grid:
                        sweep = sweep_conventions(records, config, tables, excel_df, grid)
                        st.success("추천 조합: " + ", ".join(
                            f"{k}={sweep.best[k]}" for k in grid) + f"  (일치율 {sweep.best['일치율(%)']}%)")
                        st.dataframe(sweep.table, width='stretch', hide_index=True)


# --- 탭4: 적용 가정·탈퇴율 -------------------------------------------------
with tab4:
    st.subheader("적용 가정 및 탈퇴율 테이블")
    st.caption(f"현재 선택 버전: **{inputs['dec_version']}** — 사이드바 '탈퇴율 테이블 → 버전'에서 변경")

    # === 버전 업로드/관리 ===
    with st.expander("📥 탈퇴율 테이블 업로드 · 버전 관리", expanded=False):
        st.markdown(
            "퇴직·사망률 테이블을 **엑셀/CSV로 올려 버전(연도 등)으로 저장**합니다. "
            "일부만 올리면 나머지는 기본 테이블에서 자동 복사됩니다."
        )
        _base = Path(CONFIG_DIR) / "decrement_tables"
        st.caption("먼저 템플릿을 받아 값만 수정해서 올리면 형식 오류가 없습니다:")
        tp = st.columns(3)
        _labels = {"retirement_by_age": "연령별 퇴직률", "retirement_by_service": "근속별 퇴직률", "mortality": "사망률"}
        for col, (kind, fname) in zip(tp, ts.STD_FILES.items()):
            f = _base / fname
            if f.exists():
                col.download_button(f"⬇ {_labels[kind]} 템플릿", f.read_bytes(),
                                    file_name=fname, key=f"tpl_{kind}", width='stretch')

        st.divider()
        new_name = st.text_input("버전 이름", placeholder="예: 2025 또는 2025_경험생명표")
        desc = st.text_input("설명 (선택)", placeholder="예: 2025년 감독원 경험률 반영")
        up = {
            "retirement_by_age": st.file_uploader("연령별 퇴직률 (컬럼: age, rate)", type=["xlsx", "csv"], key="up_age"),
            "retirement_by_service": st.file_uploader("근속별 퇴직률 (컬럼: service, rate)", type=["xlsx", "csv"], key="up_svc"),
            "mortality": st.file_uploader("사망률 (컬럼: age, male_qx, female_qx)", type=["xlsx", "csv"], key="up_mort"),
        }
        if st.button("💾 이 버전 저장", type="primary"):
            try:
                provided = {}
                for kind, u in up.items():
                    dfu = _read_upload_table(u)
                    if dfu is not None:
                        provided[kind] = ts.normalize_table(dfu, kind)   # 미리 검증
                if not new_name.strip():
                    st.error("버전 이름을 입력하세요.")
                elif not provided:
                    st.error("파일을 하나 이상 올려주세요.")
                else:
                    ts.save_version(str(CONFIG_DIR), new_name, provided,
                                    description=desc, created=dt.date.today().isoformat())
                    st.success(f"버전 '{new_name.strip()}' 저장 완료! 사이드바에서 선택하면 적용됩니다.")
                    st.info("사이드바 '탈퇴율 테이블 → 버전' 목록을 새로고침하려면 페이지를 Rerun 하세요.")
            except Exception as e:  # noqa: BLE001 (사용자에게 원인 노출)
                st.error(f"저장 실패: {e}")

        _existing = ts.list_versions(str(CONFIG_DIR))
        if _existing:
            st.caption("등록된 버전: " + ", ".join(_existing))
            dv = st.selectbox("삭제할 버전", ["(선택 안 함)"] + _existing, key="delv")
            if dv != "(선택 안 함)" and st.button(f"🗑 '{dv}' 삭제"):
                ts.delete_version(str(CONFIG_DIR), dv)
                st.warning(f"'{dv}' 삭제됨. 페이지를 Rerun 하세요.")

    st.divider()
    st.caption("아래는 현재 선택된 버전에서 실제 적용되는 값입니다.")

    # 1) 승급율(임금상승률) 적용 여부
    g = config.salary_increase_rate.flat
    st.markdown("#### 승급율(임금상승률)")
    a1, a2, a3 = st.columns(3)
    a1.metric("연 임금상승률", f"{g*100:.2f}%")
    a2.metric("적용 여부", "✅ 적용" if g else "➖ 미적용(0%)",
              help="예상 기준급여 = 기준급여 × (1+상승률)^연차. 0%면 승급 반영 없음.")
    a3.metric("반영 시점", config.salary_increase_timing)
    yrs = list(range(0, 11))
    mult = pd.DataFrame({"예상급여배수": [(1 + g) ** y for y in yrs]}, index=yrs)
    st.caption("연차별 예상급여 배수 (기준급여=1.0 대비, 정년까지 매년 상승 반영)")
    st.line_chart(mult)

    st.divider()

    # 2) 퇴직률 테이블 (적용 기준에 따라 연령별/근속별)
    st.markdown(f"#### 퇴직률 테이블  ·  적용 기준: `{config.retirement_rate_basis}`")
    dt_cfg = config.decrement_tables
    if config.retirement_rate_basis == "age":
        ret_path, key = CONFIG_DIR / dt_cfg.retirement_by_age, "age"
    else:
        ret_path, key = CONFIG_DIR / dt_cfg.retirement_by_service, "service"
    try:
        ret_df = pd.read_csv(ret_path)
        st.caption(f"파일: `{ret_path.name}`  ({'연령별' if key=='age' else '근속별'} 퇴직률)")
        rc1, rc2 = st.columns([1, 2])
        rc1.dataframe(ret_df, hide_index=True, width='stretch', height=320)
        rc2.line_chart(ret_df.set_index(key))
    except FileNotFoundError:
        st.warning(f"퇴직률 테이블을 찾을 수 없습니다: {ret_path}")

    st.divider()

    # 3) 사망률 테이블 (성별·연령)
    st.markdown("#### 사망률 테이블 (성별·연령)")
    try:
        mort_df = pd.read_csv(CONFIG_DIR / dt_cfg.mortality)
        st.caption(f"파일: `{Path(dt_cfg.mortality).name}`")
        mc1, mc2 = st.columns([1, 2])
        mc1.dataframe(mort_df, hide_index=True, width='stretch', height=320)
        mc2.line_chart(mort_df.set_index("age"))
    except FileNotFoundError:
        st.warning("사망률 테이블을 찾을 수 없습니다.")

    st.info("⚠️ 현재 테이블은 **더미(예시) 값**입니다. 실제 경험률로 교체하려면 "
            "`config/decrement_tables/` 의 CSV 파일을 바꾸세요.")
