"""DBO 플랫폼 — 3역할 워크플로우 웹앱 (MVP).

역할: 기업 담당자(client) · 계리인(actuary) · 관리자(admin).
흐름: 기업이 명부 업로드 → 자동 검증(오류 즉시 표시) → 수정·제출 →
      계리인이 계산·보고서 확정 → 관리자가 전체 현황·이력 조회.

실행:  streamlit run app/platform_app.py

⚠️ 프로토타입: 외부 기업 고객에게 실제 개인정보를 올리게 하려면 배포 위치·
   암호화·접근통제·개인정보보호법 준수를 먼저 확정해야 합니다.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import shutil
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data" / "platform"
DB_PATH = DATA_DIR / "dbo_platform.sqlite"
FILES_DIR = DATA_DIR / "files"
ARCHIVE_DIR = DATA_DIR / "완료보고서"          # 산출일 기준 완료 보고서 보관

from dbo.census import Severity, detect_sensitive_columns, load_census, validate_census  # noqa: E402
from dbo import census_templates as CT  # noqa: E402
from dbo.config import Config  # noqa: E402
from dbo.decrement import DecrementTables  # noqa: E402
from dbo.engine import calculate_census  # noqa: E402
from dbo.outputs import write_outputs  # noqa: E402
from dbo.platform import ROLE_LABELS, auth, mailer, seed, store  # noqa: E402
from dbo.actuary_checks import (run_actuary_checks, run_aux_cross_checks,  # noqa: E402
                                run_cross_year_checks)
from dbo.smart_checks import run_smart_checks  # noqa: E402
from dbo import census_aux  # noqa: E402
from dbo import base_rates as BR  # noqa: E402
from dbo import discount as DISC  # noqa: E402
from dbo import experience as EXP  # noqa: E402
from dbo import dc as DCX  # noqa: E402
from dbo import census_review as CR  # noqa: E402
from dbo import aux_forms as AF  # noqa: E402

COLMAP = str(CONFIG_DIR / "column_map_sample.yaml")

# 테스트 기간 간소화 모드 —
#   메인(로그인) 화면: 제목 + 로그인만 노출(인트로 4대 기능 카드 숨김)
#   기업 좌측 업무메뉴: 'IFRS 1019 부채관리'만 노출(ALM·재정검증·AI·DC·컨설팅 숨김)
# 나머지 기능은 코드에 그대로 보존되며, False로 바꾸면 전체 화면이 복원된다.
TEST_SIMPLE_MODE = True

st.set_page_config(page_title="WIKI 퇴직연금 관리 시스템", page_icon="🏢", layout="wide")


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def eok(v) -> str:
    return f"{(v or 0)/1e8:,.1f}억"


def _idx(options, value) -> int:
    return options.index(value) if value in options else 0


def render_qa_thread(company_id: int, author_role: str, author_id: int):
    """질의응답 게시판 — 질문번호(Q#)+제목, 답변은 질문 아래 중첩."""
    threads = store.list_qa_threads(DB_PATH, company_id)
    st.markdown(f"**📋 질의응답 게시판** · 총 {len(threads)}건")

    # 기업: 제목 + 내용으로 질문 등록
    if author_role == "client":
        with st.form(f"qa_ask_{company_id}", clear_on_submit=True):
            title = st.text_input("제목", key=f"qtitle_{company_id}", placeholder="질문 제목")
            body = st.text_area("내용", key=f"qbody_{company_id}", placeholder="질문 내용")
            if st.form_submit_button("📝 질문 등록", type="primary") and body.strip():
                store.add_question(DB_PATH, company_id, author_id,
                                   title.strip() or "(제목 없음)", body.strip(), now())
                st.rerun()

    if not threads:
        st.caption("아직 등록된 질문이 없습니다.")
        return
    st.caption("최신 질문순 · 과거 이력 전체 조회")
    for q in threads:
        qno = q.get("qno") or q.get("id")
        with st.container(border=True):
            answers = q.get("answers", [])
            chip = ("<span style='background:#2e7d32;color:#fff;padding:1px 8px;border-radius:10px;"
                    "font-size:0.78em'>✅ 답변완료</span>" if answers else
                    "<span style='background:#c77700;color:#fff;padding:1px 8px;border-radius:10px;"
                    "font-size:0.78em'>⏳ 답변대기</span>")
            h1, h2 = st.columns([3, 2])
            h1.markdown(f"**[Q{qno}] {q.get('title') or '(제목 없음)'}**  {chip}",
                        unsafe_allow_html=True)
            h2.markdown(f"<div style='text-align:right;color:#8a8a8a;font-size:0.85em'>"
                        f"🏢 {q.get('author_name') or '기업'} · {q['created'][:16].replace('T', ' ')}</div>",
                        unsafe_allow_html=True)
            st.write(q["body"])
            for a in answers:
                st.markdown(f"&nbsp;&nbsp;&nbsp;↳ **🧮 계리인 답변** "
                            f"<span style='color:#8a8a8a;font-size:0.85em'>· {a['created'][:16].replace('T', ' ')}</span>",
                            unsafe_allow_html=True)
                st.info(a["body"])
            if author_role == "actuary":
                with st.form(f"ans_{q['id']}", clear_on_submit=True):
                    ans = st.text_area(f"Q{qno} 답변 작성", key=f"ansbody_{q['id']}")
                    if st.form_submit_button(f"↳ Q{qno} 답변 등록") and ans.strip():
                        store.add_answer(DB_PATH, company_id, author_id, q["id"], ans.strip(), now())
                        st.rerun()
            elif not answers:
                st.caption("⏳ 답변 대기 중")


def _render_qa_panel(company_id: int, role: str, author_id: int):
    """질의응답 패널 — 상단 요약(전체/완료/대기) + 게시판."""
    threads = store.list_qa_threads(DB_PATH, company_id)
    pend = sum(1 for q in threads if not q.get("answers"))
    st.markdown("### 💬 질의응답")
    if threads:
        m1, m2, m3 = st.columns(3)
        m1.metric("전체 질문", len(threads))
        m2.metric("답변 완료", len(threads) - pend)
        m3.metric("답변 대기", pend)
    with st.container(border=True):
        render_qa_thread(company_id, role, author_id)


def build_audit_qa_for(sub) -> list:
    """제출 건의 저장된 실행로그·명부로 감사 대응 Q&A를 재생성한다."""
    res = store.latest_result(DB_PATH, sub["id"])
    if not res or not res.get("run_log_path"):
        return []
    from dbo.audit_qa import build_audit_qa
    log = json.loads(Path(res["run_log_path"]).read_text(encoding="utf-8"))
    cfg = Config.model_validate(log["config_snapshot"])
    records, _rep, _df = load_census(sub["stored_path"], column_map=COLMAP)
    tables = DecrementTables.from_config(cfg, base_dir=str(CONFIG_DIR))
    result_obj = calculate_census(records, cfg, tables, with_detail=False)
    pi = store.get_plan_info(DB_PATH, sub["company_id"])
    pr = store.get_prior_record(DB_PATH, sub["company_id"])
    return build_audit_qa(records, cfg, tables, result_obj, plan_info=pi, prior=pr)


def _render_audit_qa(sub, key_prefix, expanded=False, editable=False):
    """감사 대응 Q&A(보고서 작성내용 설명) 렌더 — 계리사·기업 공통.

    key_prefix로 위젯/세션 키를 분리해 계리사·기업 화면에서 함께 쓸 수 있다.
    editable=True(계리사)면 맨 아래 계리사 코멘트를 작성·저장할 수 있고, 기업 화면에는
    저장된 코멘트가 읽기 전용으로 표시된다.
    """
    sid = sub["id"]
    with st.expander("📋 감사 대응 Q&A — 보고서 작성 내용 설명 (표준 답변·근거)", expanded=expanded):
        if not store.latest_result(DB_PATH, sid):
            st.info("계산 완료 후 생성할 수 있습니다.")
            return
        if st.button("생성 / 새로고침", key=f"genqa_{key_prefix}"):
            st.session_state[f"aqa_{key_prefix}"] = build_audit_qa_for(sub)
        qa = st.session_state.get(f"aqa_{key_prefix}")
        if qa:
            for item in qa:
                st.markdown(f"**Q. {item['q']}**")
                st.write(item["a"])
                st.caption(f"근거: {item['basis']}")
            md = "\n\n".join(f"## Q. {i['q']}\n\n{i['a']}\n\n> 근거: {i['basis']}" for i in qa)
            if sub.get("audit_comment"):
                md += f"\n\n---\n\n## 계리사 코멘트\n\n{sub['audit_comment']}"
            st.download_button("📥 Q&A 내려받기(.md)", md.encode("utf-8"),
                               file_name=f"{sub['company_name']}_감사대응QA.md",
                               key=f"dlqa_{key_prefix}")
        else:
            st.caption("‘생성 / 새로고침’을 누르면 이 산출건 보고서에 대한 설명 Q&A가 만들어집니다.")

        # ── 맨 아래: 계리사 코멘트 (계리사 작성 · 기업 열람) ──
        st.divider()
        st.markdown("**🖊 계리사 코멘트**")
        cur = sub.get("audit_comment") or ""
        if editable:
            txt = st.text_area("보고서 관련 계리사 코멘트 (저장하면 기업 화면에도 함께 표시됩니다)",
                               value=cur, key=f"aqacmt_{key_prefix}", height=110,
                               placeholder="예: 이번 산출은 개발원2312 기초율·AA+ 커브를 적용했으며, "
                                           "전기 대비 증가는 임금상승률 가정 변경이 주 원인입니다.")
            if st.button("💾 코멘트 저장", key=f"aqacmtsave_{key_prefix}"):
                store.set_audit_comment(DB_PATH, sid, txt.strip(), now())
                st.success("계리사 코멘트를 저장했습니다.")
                st.rerun()
        elif cur:
            st.info(cur)
        else:
            st.caption("아직 등록된 계리사 코멘트가 없습니다.")


# 최초 실행 시 데모 데이터 시드
seed.seed_if_empty(DB_PATH, now())


# ---------------------------------------------------------------------------
# 공통: 검증 문제 행을 명부 레이아웃으로
# ---------------------------------------------------------------------------

def _issue_match(issue, only: str) -> bool:
    if only == "error":
        return issue.severity == Severity.ERROR
    if only == "warning":
        return issue.severity == Severity.WARNING
    return True


def problem_view(df: pd.DataFrame, report, only: str = "all") -> pd.DataFrame:
    id_to_rows: dict = {}
    if "emp_id" in df.columns:
        for idx, v in df["emp_id"].items():
            id_to_rows.setdefault(None if pd.isna(v) else str(v).strip(), []).append(idx)
    problems: dict = {}
    for issue in report.issues:
        if not _issue_match(issue, only):
            continue
        tag = "🔴오류" if issue.severity == Severity.ERROR else "🟡경고"
        rows = [issue.row] if issue.row is not None else id_to_rows.get(
            str(issue.emp_id) if issue.emp_id is not None else None, [])
        for r in rows:
            problems.setdefault(r, []).append(f"{tag} {issue.message}")
    idxs = sorted(i for i in problems if i in df.index)
    if not idxs:
        return pd.DataFrame()
    view = df.loc[idxs].copy()
    view.insert(0, "엑셀행", [i + 2 for i in idxs])
    view.insert(1, "⚠문제", [" / ".join(problems[i]) for i in idxs])
    return view


def _issues_df(report, only: str = "all") -> pd.DataFrame:
    """이슈 단위 목록(심각도·규칙·엑셀행·사번·내용) — 행에 매칭 안 되는 컬럼 오류도 포함."""
    rows = []
    for i in report.issues:
        if not _issue_match(i, only):
            continue
        rows.append({
            "심각도": "🔴오류" if i.severity == Severity.ERROR else "🟡경고",
            "규칙": getattr(i, "rule", "") or "",
            "엑셀행": (i.row + 2) if getattr(i, "row", None) is not None else "-",
            "사번": i.emp_id if getattr(i, "emp_id", None) is not None else "-",
            "내용": i.message,
        })
    return pd.DataFrame(rows)


def _df_to_xlsx_bytes(df: pd.DataFrame, sheet: str = "Sheet1") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        (df if not df.empty else pd.DataFrame({"안내": ["해당 없음"]})).to_excel(
            w, sheet_name=sheet[:28], index=False)
    return buf.getvalue()


def _row_issue_map(mapped_df: pd.DataFrame, report) -> dict:
    """행 인덱스별 {구분(오류/경고), 추정오류(사유 합침)}. 원본 양식 주석용."""
    id_to_rows: dict = {}
    if "emp_id" in mapped_df.columns:
        for idx, v in mapped_df["emp_id"].items():
            id_to_rows.setdefault(None if pd.isna(v) else str(v).strip(), []).append(idx)
    per: dict = {}
    for issue in report.issues:
        rows = [issue.row] if issue.row is not None else id_to_rows.get(
            str(issue.emp_id) if issue.emp_id is not None else None, [])
        tag = "🔴오류" if issue.severity == Severity.ERROR else "🟡경고"
        for r in rows:
            d = per.setdefault(int(r), {"msgs": [], "err": False})
            d["msgs"].append(f"{tag} {issue.message}")
            if issue.severity == Severity.ERROR:
                d["err"] = True
    return {r: {"구분": "오류" if d["err"] else "경고", "추정오류": " / ".join(d["msgs"])}
            for r, d in per.items()}


def _error_review_ui(df: pd.DataFrame, report, key_prefix: str, dropped=None, summary=None,
                     raw_df=None):
    """명부 검토 UI — 표준변환 확인·개인정보 안내·오류검토·통합 검토파일 다운로드."""
    dropped = dropped or []
    summary = summary or {}
    n_err, n_warn = len(report.errors), len(report.warnings)

    # ★ 올린 원본 양식 그대로 + 추정오류 표시로 내려받기 (바로 고쳐 재업로드용)
    if raw_df is not None and (n_err or n_warn):
        rim = _row_issue_map(df, report)
        annotated = CR.annotate_original(raw_df, rim)
        st.download_button("📥 오류명부 내려받기 (내가 올린 양식 그대로 + 추정오류 표시)",
                           _df_to_xlsx_bytes(annotated, "오류명부"),
                           file_name="오류명부_원본양식.xlsx", key=f"origerr_{key_prefix}",
                           type="primary")
        st.caption("원본 양식 그대로라 바로 고쳐 다시 올릴 수 있습니다. "
                   "'추정오류' 열의 사유를 보고 고치거나, 오류가 아니면 '오류가 아닐시 사유 기록' 열에 적어주세요.")

    # ① 우리 표준양식으로 어떻게 인식했는지 (기업 확인용)
    std = CR.standard_view(df)
    with st.expander("🔄 우리 표준양식으로 이렇게 인식했습니다 (값 확인)", expanded=False):
        if not std.empty:
            st.dataframe(std.head(15), width="stretch", hide_index=True)
            st.caption(f"총 {len(std)}행 · 위는 미리보기 15행")
        else:
            st.caption("표준양식으로 변환할 컬럼을 찾지 못했습니다.")

    # ② 개인정보 자동삭제 안내
    if dropped:
        st.info(f"🔒 개인정보로 판단해 **자동 삭제**한 컬럼: **{', '.join(dropped)}** — "
                "우리는 사원번호만 사용합니다.")

    # ③ 통합 검토파일(표준변환+오류검토+안내) 내려받기
    idf_all = _issues_df(report, "all")
    pv_all = problem_view(df, report, "all")
    review_wb = CR.build_review_workbook(
        df, pv_all, idf_all, dropped,
        {"records": report.n_records, "errors": n_err, "warnings": n_warn, **summary})
    st.download_button("📥 통합 검토파일 내려받기 (①표준변환 ②오류검토 ③오류목록 ④안내)",
                       review_wb, file_name="명부_검토파일.xlsx",
                       key=f"revwb_{key_prefix}", type="primary")
    st.caption("이 파일의 '②오류검토' 시트에서 잘못된 값은 고쳐서 다시 올리거나, "
               "오류가 아니면 '오류아님_사유'를 적어 제출하세요.")

    if n_err == 0 and n_warn == 0:
        st.success("✅ 오류·경고 없음")
        return
    st.markdown(f"오류 **{n_err}건** · 경고 **{n_warn}건**")
    pick = st.radio("보기", ["🔴 오류만", "🟡 경고만", "전체"], horizontal=True,
                    key=f"errpick_{key_prefix}")
    only = {"🔴 오류만": "error", "🟡 경고만": "warning", "전체": "all"}[pick]

    idf = _issues_df(report, only)
    st.markdown(f"**⚠️ 오류/경고 목록 ({len(idf)}건)** — 무엇이·어디서(엑셀행/사번) 잘못됐는지")
    if not idf.empty:
        st.dataframe(idf, width="stretch", hide_index=True)
        st.download_button("📥 오류목록 내려받기 (xlsx)", _df_to_xlsx_bytes(idf, "오류목록"),
                           file_name="명부_오류목록.xlsx", key=f"errdl_{key_prefix}")
    else:
        st.caption("해당 항목이 없습니다.")

    pv = problem_view(df, report, only)
    if not pv.empty:
        st.markdown("**🔎 문제가 있는 행 (원본 명부 + 문제내용)**")
        st.dataframe(pv, width="stretch", hide_index=True)
        st.download_button("📥 오류 행 명부 내려받기 (xlsx)", _df_to_xlsx_bytes(pv, "오류행"),
                           file_name="명부_오류행.xlsx", key=f"errrowdl_{key_prefix}")


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------

_INTRO_MODULES = [
    ("📐", "IFRS 1019 퇴직급여 부채 관리",
     ["개인별 예측단위적립방식(PUC) 부채 산출",
      "할인율: AA급 회사채 커브 기반 단일할인율",
      "기초 DBO · 당기근무원가 · 과거근무원가 · 이자비용 · 급여지급 · 재측정(OCI)",
      "손익(PL)/기타포괄손익(OCI) 자동 분리, 분개·ERP 연계",
      "공시 주석·감사 증빙 자료 자동 생성"]),
    ("⚖️", "ALM · 자산부채관리",
     ["IFRS 부채 기준 지급시점별 현금흐름 재구성",
      "부채 듀레이션 · PV01 산출",
      "사외적립자산 포트폴리오·수익률·리스크 현황",
      "듀레이션 갭 · 현금흐름 매칭률(1/3/5년) · 부족 구간 식별"]),
    ("🧪", "재정검증 (Funding Test)",
     ["5년 · 10년 · 20년 적립비율 전망",
      "자금 부족 발생 시점 · 추가 적립 필요액",
      "시나리오 분석: 기대수익률 · 임금상승률 · 신규채용 · 적립정책"]),
    ("🤖", "AI 의사결정 지원",
     ["가정 적정성 평가 (과거 추세·시장 데이터 대비 점수)",
      "명부·급여 이상치 탐지 및 조치 이력 관리",
      "ALM 전략 추천 · 재정위험 조기경보",
      "IFRS 주석·경영진 보고·감사 대응 문구 자동 생성"]),
]


def render_login():
    st.markdown(
        """
        <div style="padding:26px 30px;border-radius:16px;margin-bottom:8px;
             background:linear-gradient(120deg,#1F4E79 0%,#2E6DA4 55%,#3f8fd0 100%);color:#fff;">
          <div style="font-size:13px;letter-spacing:3px;opacity:.85;">WIKISOFT · RETIREMENT PENSION SYSTEM</div>
          <div style="font-size:30px;font-weight:800;margin-top:4px;">WIKI 퇴직연금 관리 시스템</div>
          <div style="font-size:15px;margin-top:6px;opacity:.92;">
            기업 담당자 · 계리사 · 퇴직연금전문기업 WIKI가 함께 쓰는 협업 시스템</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("프로토타입 미리보기")

    # 4대 기능 영역 카드 (2 × 2) — 테스트 간소화 모드에서는 숨긴다.
    if not TEST_SIMPLE_MODE:
        for i in range(0, len(_INTRO_MODULES), 2):
            cols = st.columns(2)
            for col, (icon, title, items) in zip(cols, _INTRO_MODULES[i:i + 2]):
                with col.container(border=True, height=250):
                    st.markdown(f"#### {icon} {title}")
                    st.markdown("\n".join(f"- {x}" for x in items))

    st.divider()
    users = auth.list_users(DB_PATH)
    if not users:
        st.error("등록된 사용자가 없습니다. 앱을 다시 실행해 주세요.")
        return

    # 프로토타입: 오타·한/영 문제를 없애기 위해 아이디를 목록에서 고른다(비밀번호 없음).
    def _label(acc):
        who = acc["display_name"] or acc["username"]
        role = ROLE_LABELS.get(acc["role"], acc["role"])
        comp = f" · {acc['company_name']}" if acc.get("company_name") else ""
        return f"{who}  ({role}{comp}) — {acc['username']}"

    with st.form("login"):
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        idx = c1.selectbox(
            "🔐 로그인 — 사용자 선택",
            options=list(range(len(users))),
            format_func=lambda i: _label(users[i]),
        )
        if c2.form_submit_button("로그인", type="primary", width="stretch"):
            st.session_state["user"] = users[idx]
            st.rerun()
    st.caption("프로토타입 단계에서는 비밀번호 없이 아이디 선택만으로 로그인합니다. "
               "접속자(아이디)는 이력에 그대로 기록됩니다.")


if "user" not in st.session_state:
    render_login()
    st.stop()

user = st.session_state["user"]
st.sidebar.title("🏢 WIKI 퇴직연금 관리 시스템")
st.sidebar.write(f"**{user['display_name']}**")
st.sidebar.caption(f"{ROLE_LABELS[user['role']]}"
                   + (f" · {user['company_name']}" if user.get("company_name") else ""))
if st.sidebar.button("로그아웃"):
    del st.session_state["user"]
    st.rerun()

# 기업 전용 좌측 업무 메뉴 — (아이콘, 제목, 활성화 여부). 로그아웃 아래에 배치.
_CLIENT_MENU = [
    ("📐", "IFRS 1019 퇴직급여 부채 관리", True),
    ("⚖️", "ALM · 자산부채관리", False),
    ("🧪", "재정검증 (Funding Test)", False),
    ("🤖", "AI 의사결정 지원", False),
    ("🗂", "DC형 업무처리지원", False),
    ("🏢", "DB형 운영 컨설팅 (기업)", False),
    ("🧑‍💼", "DC·IRP 가입 컨설팅 (종업원)", False),
]
if user["role"] == "client":
    # 테스트 간소화 모드: IFRS 1019만 노출(나머지 메뉴 숨김) + 항상 메인으로 고정
    _menu_items = _CLIENT_MENU[:1] if TEST_SIMPLE_MODE else _CLIENT_MENU
    if TEST_SIMPLE_MODE:
        st.session_state["client_menu"] = 0
    st.sidebar.divider()
    st.sidebar.markdown("##### 업무 메뉴")
    _cmenu_sel = st.session_state.get("client_menu", 0)
    for _i, (_icon, _title, _active) in enumerate(_menu_items):
        if st.sidebar.button(f"{_icon} {_title}", key=f"cmenu_{_i}", width="stretch",
                             type=("primary" if _i == _cmenu_sel else "secondary")):
            st.session_state["client_menu"] = _i
            st.rerun()
        # 모든 항목에 캡션을 달아 버튼 간 간격을 일정하게 유지
        if not TEST_SIMPLE_MODE:
            st.sidebar.caption("　✅ 사용 가능" if _active else "　🧪 설계안(미리보기)")

st.sidebar.divider()
st.sidebar.caption("⚠️ 실데이터(개인정보)는 배포·보안 확정 전 업로드 금지")


# ---------------------------------------------------------------------------
# 기업 담당자 (client)
# ---------------------------------------------------------------------------

CALCULATOR_FIRM = "위키소프트"
PURPOSES = ["IFRS-1019(재무제표)", "재정검증", "기타"]
BENEFIT_RULES = ["법정제", "누진제", "기타"]
UNDER1YR_METHODS = ["일할", "월할(절상)", "월할(절하)"]
DISCOUNT_BASES = ["AAA", "AA+", "AA0", "AA-", "A+", "A0", "기타"]
# 사외적립자산 적립비율(가입비율) 선택지 — 기타 선택 시 직접 입력
FUNDING_RATIO_OPTIONS = ["100%", "90%", "80%", "70%", "60%", "50%", "기타"]
DISCOUNT_RATE_NOTE = (
    "※ 퇴직급여채무 산정시 사용되는 할인율은 [기업회계기준서 제1019호]에 의거, "
    "보고기간 말 현재 우량회사채 시장수익률 또는 국공채의 시장수익률을 사용합니다.\n"
    "　　· 기존에 평가를 받은 적이 있는 경우는 평가보고서에서 확인 가능합니다.\n"
    "　　· 기존에 평가를 받은 적이 없는 경우는 회계사(감사인)께 문의하시면 확인 가능합니다."
)
# 탈퇴·지급 시점(할인 기준) — 연중(mid_year)이 한국 실무 표준·기본값
TIMING_OPTS = ["연중(mid-year) — 권장", "연말(end-of-year)"]
DEFAULT_TIMING = "mid_year"
# 사외적립자산 현황 항목 (엑셀 '3. 사외적립자산 현황' — 산출기준일 단일 입력)
FUNDING_ITEMS = [
    ("기초잔액", "기초 잔액 (A)"), ("입금액", "입금(수탁)액 (B)"),
    ("지급_퇴직", "급여지급(인출) - 퇴직"), ("지급_중간정산", "급여지급 - 중간정산"),
    ("지급_DC전환", "급여지급 - DC전환"),
    ("관계사전입", "관계사 전입액"), ("관계사전출", "관계사 전출액"),
    ("사업결합", "사업결합액"), ("사업처분", "사업처분액"),
    ("투자수익", "운용성과 - 투자수익"), ("운용수수료", "운용성과 - 운용관리수수료"),
    ("기말_퇴직연금", "기말잔액 - 퇴직연금"), ("기말_퇴직신탁", "기말잔액 - 퇴직신탁"),
    ("기말_퇴직보험", "기말잔액 - 퇴직보험"), ("국민연금전환금", "국민연금전환금"),
    ("세부_현금", "세부내역 - 현금및현금등가물"), ("세부_지분", "세부내역 - 지분상품"),
    ("세부_채권", "세부내역 - 채권"), ("세부_부동산", "세부내역 - 부동산"),
    ("세부_기타", "세부내역 - 기타자산"),
]
DISCLOSURE_METHODS = ["② 사외적립자산에 포함", "① 별도 공시", "③ 미공시"]


def page_client(user):
    sel = st.session_state.get("client_menu", 0)
    st.markdown(f"## ㈜ {user['company_name']}")
    pages = [_client_main, _client_alm, _client_funding, _client_ai, _client_dc,
             _client_db_consult, _client_irp_consult]
    pages[sel](user)


# ── 부가 모듈 (설계안 · 시연용 예시 화면) — 실제 연동 전 화면 구성/아웃풋 미리보기 ──
def _design_banner(title, desc):
    st.markdown(f"### {title}")
    st.info(f"🧪 **설계안 · 시연용 예시 화면** — {desc}\n\n"
            "아래 지표·그래프·표는 **샘플(예시) 데이터**입니다. 화면 구성·항목을 보시고 "
            "원하는 형태로 알려주시면 그대로 바꿔드립니다. (실제 데이터 연동·계산은 이후 단계)")


def _demo_liability(user):
    """예시 부채 기준값 — 회사에 산출결과가 있으면 그 DBO, 없으면 예시 50억."""
    for s in store.list_submissions(DB_PATH, company_id=user["company_id"]):
        r = store.latest_result(DB_PATH, s["id"])
        if r and r.get("total_dbo"):
            return float(r["total_dbo"]), s["valuation_date"]
    return 5_000_000_000.0, None


def _client_alm(user):
    _design_banner("⚖️ ALM · 자산부채관리",
                   "IFRS 부채 현금흐름과 사외적립자산을 매칭해 듀레이션 갭·현금흐름 부족구간을 봅니다.")
    liab, vdate = _demo_liability(user)
    assets = liab * 0.62
    st.caption(f"부채 기준: {'최근 산출 DBO (' + vdate + ')' if vdate else '예시값 50억'} · "
               "자산은 예시 적립비율 62% 가정")
    k = st.columns(4)
    k[0].metric("확정급여채무(DBO)", eok(liab))
    k[1].metric("사외적립자산", eok(assets))
    k[2].metric("적립비율", f"{assets / liab * 100:.1f}%")
    k[3].metric("순확정급여부채", eok(liab - assets))
    k2 = st.columns(4)
    k2[0].metric("부채 듀레이션", "8.7년")
    k2[1].metric("자산 듀레이션", "3.2년")
    k2[2].metric("듀레이션 갭", "＋5.5년", delta="자산이 짧음", delta_color="inverse")
    k2[3].metric("PV01(부채·1bp)", eok(liab * 8.7 * 1e-4))

    years = list(range(1, 21))
    pay = [liab * 0.095 * (0.93 ** (y - 1)) for y in years]
    acf = [assets * 0.12 * (0.86 ** (y - 1)) for y in years]
    st.markdown("##### 📉 연도별 현금흐름 매칭 (부채 예상지급액 vs 자산 예상현금흐름)")
    st.bar_chart(pd.DataFrame({"부채 예상지급액": pay, "자산 예상현금흐름": acf}, index=years))

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### 만기구간 현금흐름 매칭률")
        rows = []
        for lbl, a, b in [("1년 이내", 0, 1), ("1~3년", 1, 3), ("3~5년", 3, 5),
                          ("5~10년", 5, 10), ("10년 초과", 10, 20)]:
            p, c = sum(pay[a:b]), sum(acf[a:b])
            rows.append({"만기구간": lbl, "부채지급(억)": round(p / 1e8, 1),
                         "자산유입(억)": round(c / 1e8, 1),
                         "매칭률": f"{(c / p * 100 if p else 0):.0f}%"})
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with c2:
        st.markdown("##### 사외적립자산 포트폴리오 (예시)")
        port = pd.DataFrame({"자산군": ["채권", "주식", "대체투자", "현금성"],
                             "비중(%)": [55, 20, 15, 10],
                             "기대수익률(%)": [3.2, 6.5, 5.0, 1.5]})
        st.dataframe(port, width="stretch", hide_index=True)
        st.bar_chart(port.set_index("자산군")["비중(%)"])

    st.markdown("##### 🧭 해설·시사점 (예시)")
    st.markdown(
        "- **듀레이션 갭 +5.5년**: 부채가 자산보다 길어 금리 하락 시 순부채 증가 위험이 큽니다. "
        "장기채 편입으로 자산 듀레이션을 늘리는 방향을 검토하세요.\n"
        "- **단기(1년 이내) 매칭률 점검**: 단기 지급 대비 현금성 자산 비중 확인.\n"
        "- **적립비율 62%**: 재정검증(Funding Test)에서 목표 적립비율 도달 시점을 확인하세요.")


def _client_funding(user):
    _design_banner("🧪 재정검증 (Funding Test)",
                   "적립비율 20년 전망과 자금부족 시점·추가적립 필요액을 가정별로 봅니다.")
    liab, vdate = _demo_liability(user)
    assets0 = liab * 0.62
    st.markdown("##### ⚙️ 가정 (슬라이더를 바꾸면 전망이 즉시 재계산됩니다)")
    c = st.columns(4)
    ret = c[0].slider("자산 기대수익률(%)", 0.0, 8.0, 3.5, 0.5, key="ft_ret")
    contrib = c[1].slider("연간 적립액(부채 대비 %)", 0.0, 15.0, 5.0, 0.5, key="ft_con")
    wage = c[2].slider("임금상승률(%)", 0.0, 8.0, 4.0, 0.5, key="ft_wage")
    target = c[3].slider("목표 적립비율(%)", 60, 120, 100, 5, key="ft_tgt")

    years = list(range(0, 21))
    fr, a, l = [], assets0, liab
    for _y in years:
        fr.append(a / l * 100)
        a = a * (1 + ret / 100) + l * contrib / 100
        l = l * (1 + wage / 100 * 0.35)
    st.markdown("##### 📈 적립비율 20년 전망")
    st.line_chart(pd.DataFrame({"적립비율(%)": fr}, index=years))

    reach = next((y for y, v in zip(years, fr) if v >= target), None)
    k = st.columns(4)
    k[0].metric("현재 적립비율", f"{fr[0]:.1f}%")
    k[1].metric("5년 후", f"{fr[5]:.1f}%")
    k[2].metric("10년 후", f"{fr[10]:.1f}%")
    k[3].metric(f"목표({target}%) 도달", f"{reach}년차" if reach is not None else "20년 내 미달")

    st.markdown("##### 자금 부족·추가적립 필요액 (목표 대비 · 예시)")
    rows, a2, l2 = [], assets0, liab
    for y in years:
        if y in (1, 3, 5, 10, 20):
            need = max(0.0, l2 * target / 100 - a2)
            rows.append({"시점": f"{y}년차", "부채(억)": round(l2 / 1e8, 1),
                         "자산(억)": round(a2 / 1e8, 1), "적립비율": f"{a2 / l2 * 100:.0f}%",
                         f"목표({target}%) 부족액(억)": round(need / 1e8, 1)})
        a2 = a2 * (1 + ret / 100) + l2 * contrib / 100
        l2 = l2 * (1 + wage / 100 * 0.35)
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    if reach is None:
        st.warning("현재 가정으로는 20년 내 목표 적립비율에 도달하지 못합니다 — 적립액 상향·수익률 개선 검토.")
    else:
        st.success(f"현재 가정으로 약 **{reach}년차**에 목표 적립비율({target}%)에 도달합니다.")


def _client_ai(user):
    _design_banner("🤖 AI 의사결정 지원",
                   "가정 적정성 평가·이상치 탐지·재정위험 조기경보·문구 자동생성을 제공합니다.")
    st.markdown("##### 🎯 가정 적정성 평가 (과거추세·시장 벤치마크 대비 · 예시)")
    st.dataframe(pd.DataFrame({
        "가정": ["할인율", "임금상승률", "퇴직률", "승급률"],
        "현재값": ["4.20%", "4.00%", "개발원2312", "3.00%"],
        "적정범위(예시)": ["3.9~4.5%", "3.5~4.5%", "표준", "2.5~3.5%"],
        "적정성 점수": [88, 72, 95, 65],
        "판정": ["적정", "적정", "우수", "검토 필요"],
    }), width="stretch", hide_index=True)
    st.caption("점수는 과거 추세·시장 벤치마크 대비 편차를 0~100으로 환산한 예시입니다.")

    st.markdown("##### 🚨 재정위험 조기경보 (예시)")
    w = st.columns(3)
    w[0].metric("종합 위험도", "보통", delta="주의", delta_color="inverse")
    w[1].metric("적립비율 추세", "하락", delta="-3%p/년", delta_color="inverse")
    w[2].metric("듀레이션 갭", "+5.5년", delta="확대", delta_color="inverse")

    st.markdown("##### 🔎 명부·급여 이상치 탐지 (예시)")
    st.dataframe(pd.DataFrame({
        "구분": ["급여 급변", "연령 이상", "근속 역전", "중복 사번"],
        "탐지 건수": [3, 1, 2, 0],
        "설명": ["전기 대비 ±30% 초과", "정년 초과 재직", "입사일 > 기준일", "중복 없음"],
    }), width="stretch", hide_index=True)

    st.markdown("##### ✍️ 자동 생성 문구 (IFRS 주석·경영진 보고 초안 · 예시)")
    st.text_area("초안", height=150, key="ai_text_demo", value=(
        "당사의 확정급여채무는 예측단위적립방식으로 산정되었으며, 주요 계리적 가정인 할인율 4.20%, "
        "임금상승률 4.00%를 적용하였습니다. 전기 대비 확정급여채무 증가는 주로 할인율 하락 및 "
        "임금상승률 가정 변경에 기인합니다. 적립비율은 62%이며, 향후 5년간 단계적 적립을 통해 "
        "목표 적립비율 100% 달성을 계획하고 있습니다."))
    st.caption("실제로는 산출결과·가정에 맞춰 문구가 자동 생성됩니다(현재는 예시).")


def _client_dc(user):
    _design_banner("🗂 DC형 업무처리지원",
                   "DC 부담금(보험료) 산정 → 인별 납부액·계좌 명세서 자동 생성 → 미납·분할·운용까지. "
                   "중소기업의 DC 납부 업무 부담을 덜어줍니다.")
    st.markdown("**① 가입자 명부(임금·계좌)를 올리면 → ② 인별 부담금·납부 계좌 명세서 자동 생성 → "
                "③ 미납/분할/운용 관리**까지 한 화면에서. "
                "(계좌 등 자료는 계산·다운로드만 하고 서버에 저장하지 않습니다.)")

    c0 = st.columns([1.4, 1, 1, 2])
    c0[0].download_button("📄 가입자·계좌 명부 양식", DCX.build_dc_template(),
                          file_name="DC_가입자계좌_양식.xlsx", key="dc_tmpl", width="stretch")
    plan_year = int(c0[1].number_input("대상 연도", 2020, 2100, 2025, key="dc_year"))
    denom = int(c0[2].number_input("부담금 1/n", 1, 24, 12, key="dc_denom",
                                   help="DC 부담금 = 연간임금총액 × 1/n (법정 최소 1/12)"))
    up = c0[3].file_uploader("가입자 명부 업로드 (xlsx / csv)", type=["xlsx", "csv"], key="dc_up")

    # 산정 옵션(산식·가감·상하한·명세서 항목)
    with st.expander("⚙️ 산정 옵션 — 산식·가감·상하한·명세서 항목", expanded=False):
        o = st.columns(3)
        prorate_lbl = o[0].radio("연중 입·퇴사자 산정", ["일할(재직일수)", "월할(재직월수)"],
                                 key="dc_prorate")
        proration = "monthly" if "월할" in prorate_lbl else "daily"
        adjust = o[1].number_input("전역 가감률(계수)", value=1.0, step=0.05, key="dc_adj",
                                   help="규정상 가감(예 1.1=10% 가산). 행에 '가감률'이 있으면 그 값 우선.")
        o2 = st.columns(3)
        floor = o2[0].number_input("연간 부담금 하한(원, 0=미적용)", value=0, step=100_000, key="dc_floor")
        cap_v = o2[1].number_input("연간 부담금 상한(원, 0=미적용)", value=0, step=100_000, key="dc_cap")
        cap = cap_v if cap_v > 0 else None
        o3 = st.columns(2)
        biz_no = o3[0].text_input("사업장관리번호(명세서 표기)", key="dc_bizno")
        due_date = o3[1].text_input("납부기한(명세서 표기, 예 2025-01-31)", key="dc_due")

    if up is None:
        _dc_sample_preview()
        return

    try:
        roster = DCX.parse_dc_roster(bytes(up.getbuffer()))
        rows, summ = DCX.compute_dc_contributions(roster, plan_year, denom, proration,
                                                  adjust, float(floor), cap)
    except Exception as e:  # noqa: BLE001
        st.error(f"명부를 읽지 못했습니다: {e}")
        return
    if not rows:
        st.warning("가입자를 인식하지 못했습니다. 양식(사원번호·연간임금총액 등)을 확인하세요.")
        return

    t1, t2, t3, t4 = st.tabs(["💰 부담금·납부명세", "📆 월별 분할납부", "🧾 미납/체납 관리", "📈 운용현황"])

    with t1:
        k = st.columns(4)
        k[0].metric("가입자 수", f"{summ['n']}명")
        k[1].metric(f"{plan_year}년 총 납부액", eok(summ["total"]))
        k[2].metric("1인 평균", eok(summ["total"] / summ["n"]) if summ["n"] else "-")
        k[3].metric("계좌 미기재", f"{summ['n_missing_account']}명")
        st.caption(f"산식: 연간임금 × 1/{denom} × 가감률 {adjust}"
                   + (f" · 하한 {int(floor):,}" if floor else "")
                   + (f" · 상한 {int(cap):,}" if cap else "")
                   + f" · {prorate_lbl}")
        st.markdown("##### 📋 인별 납부 명세 (계좌별 납부액)")
        st.dataframe(pd.DataFrame([DCX._row_view(r) for r in rows]),
                     width="stretch", hide_index=True)
        if summ["n_missing_account"]:
            st.warning(f"계좌번호가 없는 가입자 {summ['n_missing_account']}명 — 납부 전 확인 필요.")
        if summ["n_missing_wage"]:
            st.warning(f"연간임금이 없는 가입자 {summ['n_missing_wage']}명 — 부담금 0으로 계산됨.")
        st.download_button(
            "📥 납부명세서 내려받기 (Excel · 은행 이체/회계 반영용)",
            DCX.build_dc_payment_xlsx(rows, summ, user["company_name"], plan_year,
                                      biz_no or None, due_date or None),
            file_name=f"{user['company_name']}_{plan_year}_DC납부명세서.xlsx",
            type="primary", key="dc_dl", width="stretch")

    with t2:
        st.caption("연간 부담금을 12개월로 균등 분할합니다(마지막 달이 잔액 흡수). 분할 납부 계획에 활용.")
        n_month = st.slider("분할 개월수", 2, 12, 12, key="dc_nmonth")
        msplit = DCX.monthly_split(rows, n_month)
        head = ["사번", "소속", "연간부담금"] + [f"{m+1}회차" for m in range(n_month)]
        data = [[r["emp_id"], r.get("dept", ""), r["contribution"], *r["monthly"]] for r in msplit]
        st.dataframe(pd.DataFrame(data, columns=head), width="stretch", hide_index=True)
        tot_by_month = [sum(r["monthly"][m] for r in msplit) for m in range(n_month)]
        st.markdown("##### 회차별 납부 합계")
        st.bar_chart(pd.DataFrame({"회차 합계": tot_by_month},
                                  index=[f"{m+1}회차" for m in range(n_month)]))

    with t3:
        st.caption("가입자별 납부 여부를 체크해 미납·체납을 관리합니다. (명부의 '납부상태' 열을 기본값으로 사용)")
        pay_df = pd.DataFrame([{
            "사번": r["emp_id"], "소속": r.get("dept", ""), "납부액": r["contribution"],
            "납부완료": (str(r.get("status", "")).replace(" ", "") in ("납부완료", "완료", "정상", "y", "Y")),
        } for r in rows])
        edited = st.data_editor(pay_df, width="stretch", hide_index=True, key="dc_payedit",
                                disabled=["사번", "소속", "납부액"])
        unpaid = edited[~edited["납부완료"]]
        m = st.columns(3)
        m[0].metric("미납 인원", f"{len(unpaid)}명")
        m[1].metric("미납 금액", eok(unpaid["납부액"].sum()))
        done = len(edited) - len(unpaid)
        m[2].metric("납부 완료율", f"{done/len(edited)*100:.1f}%" if len(edited) else "-")
        if len(unpaid):
            st.markdown("##### 미납자 목록 (독려 대상)")
            st.dataframe(unpaid[["사번", "소속", "납부액"]], width="stretch", hide_index=True)
        else:
            st.success("미납자가 없습니다. 전원 납부 완료.")

    with t4:
        have, avg, tp, tv = DCX.dc_return_stats(rows)
        if not have:
            st.info("적립원금·평가액이 입력된 가입자가 없습니다. 명부에 '적립원금'·'평가액' 열을 채우면 "
                    "인별·전체 수익률을 계산합니다.")
        else:
            k = st.columns(4)
            k[0].metric("운용대상 인원", f"{len(have)}명")
            k[1].metric("총 적립원금", eok(tp))
            k[2].metric("총 평가액", eok(tv))
            k[3].metric("가중평균 수익률", f"{avg*100:.2f}%" if avg is not None else "-")
            st.markdown("##### 가입자별 운용현황")
            st.dataframe(pd.DataFrame([{
                "사번": r["emp_id"], "소속": r.get("dept", ""),
                "적립원금": int(r["principal"]), "평가액": int(r["value"]),
                "평가손익": int(r["value"] - r["principal"]),
                "수익률": f"{r['return_rate']*100:.2f}%",
            } for r in have]), width="stretch", hide_index=True)


def _dc_sample_preview():
    st.info("아직 명부를 올리지 않았습니다. 위 **양식**을 받아 채워 올리면 아래처럼 인별 납부명세·미납·분할·운용이 "
            "만들어집니다. (아래는 예시)")
    k = st.columns(4)
    k[0].metric("가입자 수", "3명")
    k[1].metric("2025년 총 납부액", eok(8_383_562))
    k[2].metric("1인 평균", eok(2_794_521))
    k[3].metric("계좌 미기재", "0명")
    st.markdown("##### 📋 인별 납부 명세 (예시)")
    st.dataframe(pd.DataFrame([
        {"사번": "A001", "소속": "생산", "연간임금": 42_000_000, "재직일수": 365,
         "납부액(부담금)": 3_500_000, "금융기관": "OO은행", "은행코드": "011", "계좌번호": "123-456-7890"},
        {"사번": "A002", "소속": "관리", "연간임금": 36_000_000, "재직일수": 365,
         "납부액(부담금)": 3_000_000, "금융기관": "OO증권", "은행코드": "238", "계좌번호": "999-88-77665"},
        {"사번": "A003", "소속": "영업", "연간임금": 30_000_000, "재직일수": 275,
         "납부액(부담금)": 1_883_562, "금융기관": "OO은행", "은행코드": "011", "계좌번호": "111-222-33344"},
    ]), width="stretch", hide_index=True)
    st.caption("※ A003은 2025-04-01 입사자 → 재직일수(275일) 일할 산정 예시. "
               "업로드하면 월별 분할납부·미납/체납 관리·운용현황(수익률) 탭도 함께 제공됩니다.")


def _client_db_consult(user):
    _design_banner("🏢 DB형 퇴직연금 운영 컨설팅 (기업)",
                   "DB 제도 진단·제안 + 금융기관 상품 비교 · 상담 신청 · 파트너 컨설턴트.")
    t = st.tabs(["🏢 제도 진단·제안", "🏦 상품 비교", "📮 상담 신청", "👤 컨설턴트"])
    with t[0]:
        liab, vdate = _demo_liability(user)
        assets = liab * 0.62
        st.markdown("##### 📊 제도 진단")
        k = st.columns(4)
        k[0].metric("확정급여채무(DBO)", eok(liab))
        k[1].metric("사외적립자산", eok(assets))
        k[2].metric("적립비율", f"{assets / liab * 100:.0f}%", delta="목표 100% 미달", delta_color="inverse")
        k[3].metric("연간 적립부담(예상)", eok(liab * 0.08))
        st.progress(min(1.0, assets / liab), text=f"적립비율 {assets / liab * 100:.0f}% (권고 80%+)")

        st.markdown("##### ⚖️ DB 유지 vs DC 전환 비교 (예시)")
        st.dataframe(pd.DataFrame({
            "항목": ["회계부채(IFRS)", "적립부담 변동성", "운용리스크 부담", "종업원 수용성", "세제·비용"],
            "DB 유지": ["부채 인식(변동 큼)", "높음(가정 민감)", "회사 부담", "안정 선호층", "손금 한도 내"],
            "DC 전환": ["부채 축소", "낮음", "종업원 부담", "젊은층 선호", "부담금 즉시 손금"],
        }), width="stretch", hide_index=True)

        st.markdown("##### 💡 운영 최적화 제안 (예시)")
        st.markdown(
            "- **적립 정책**: 적립비율 62% → 목표 100%까지 향후 5년 단계적 적립 플랜 수립.\n"
            "- **자산운용(ALM)**: 부채 듀레이션(8.7년)에 맞춰 장기채 비중 확대로 금리위험 축소.\n"
            "- **제도 설계**: 신규 입사자 DC 적용 등 하이브리드 전환으로 부채 증가 억제 검토.\n"
            "- **회계·공시**: 할인율·임금상승률 가정 민감도 관리로 손익 변동성 완화.")

        st.markdown("##### 🗺 컨설팅 로드맵 (예시)")
        st.dataframe(pd.DataFrame({
            "단계": ["1. 진단", "2. 설계", "3. 실행", "4. 모니터링"],
            "내용": ["적립·비용·회계 진단", "적립플랜·ALM·제도 설계", "규약변경·자산배분 실행", "연간 재정검증·리밸런싱"],
            "기간": ["2주", "3주", "4주", "연간"],
        }), width="stretch", hide_index=True)
        st.caption("숫자·제안은 예시입니다. 실제 진단은 산출결과·자산현황·규정에 맞춰 생성됩니다.")
    with t[1]:
        _render_product_compare("dbp")
    with t[2]:
        _render_bulk_consult(user, "dbc", ["DB형 운영 컨설팅"])
    with t[3]:
        _render_consultants("dbcst")


def _client_irp_consult(user):
    _design_banner("🧑‍💼 DC·IRP 가입 컨설팅 (종업원)",
                   "개인 가입 시뮬레이션 + 금융기관 상품 비교 · 상담 신청 · 파트너 컨설턴트.")
    t = st.tabs(["🧮 가입 시뮬레이션", "🏦 상품 비교", "📮 상담 신청", "👤 컨설턴트"])
    with t[0]:
        c = st.columns(4)
        age = int(c[0].number_input("현재 나이", 20, 64, 35, key="irp_age"))
        retire = int(c[1].number_input("은퇴 예정 나이", min_value=age + 1, max_value=75,
                                       value=max(60, age + 1), key="irp_ret"))
        salary = int(c[2].number_input("연봉(만원)", 1000, 30000, 4200, step=100, key="irp_sal"))
        ret_rate = c[3].slider("기대 수익률(%)", 0.0, 8.0, 4.0, 0.5, key="irp_rr")
        c2 = st.columns(3)
        cur = int(c2[0].number_input("현재 DC/IRP 적립금(만원)", 0, 200000, 2000, step=100, key="irp_cur"))
        irp_month = int(c2[1].number_input("IRP 월 추가납입(만원)", 0, 200, 30, step=5, key="irp_m"))
        total_sal = int(c2[2].number_input("총급여(세액공제 기준, 만원)", 1000, 30000, salary,
                                           step=100, key="irp_tsal"))

        yrs = retire - age
        r = ret_rate / 100
        dc_annual = salary / 12          # 회사 DC 부담금(연 = 연봉×1/12)
        irp_annual = irp_month * 12      # 개인 IRP 납입(연)
        fv = cur
        curve = []
        for _y in range(yrs + 1):
            curve.append(fv)
            fv = fv * (1 + r) + dc_annual + irp_annual
        fv_ret = curve[-1]
        # 세액공제: 총급여 5,500만 이하 16.5%, 초과 13.2%. IRP 개인납입 연 한도 900만.
        ded_rate = 0.165 if total_sal <= 5500 else 0.132
        tax_save = min(irp_annual, 900) * ded_rate

        k = st.columns(4)
        k[0].metric("은퇴시점 예상 적립금", f"{fv_ret:,.0f}만원")
        k[1].metric("연금 수령(20년 분할)", f"{fv_ret / 20 / 12:,.0f}만원/월")
        k[2].metric("연 세액공제 절세", f"{tax_save:,.0f}만원")
        k[3].metric("적립 기간", f"{yrs}년")

        st.markdown("##### 📈 예상 적립금 추이")
        st.line_chart(pd.DataFrame({"예상 적립금(만원)": curve},
                                   index=list(range(age, retire + 1))))

        st.markdown("##### 🧾 세액공제 안내 (예시)")
        st.info(f"총급여 {total_sal:,}만원 → 세액공제율 **{ded_rate * 100:.1f}%**. "
                f"IRP 연 {irp_annual:,}만원 납입 시 최대 **{tax_save:,.0f}만원** 절세 "
                "(개인납입 연 한도 900만원, 퇴직연금 합산 기준). "
                f"적립 기간 {yrs}년 누적 절세 약 **{tax_save * yrs:,.0f}만원**.")

        st.markdown("##### 🎯 위험성향별 포트폴리오 제안 (예시)")
        st.dataframe(pd.DataFrame({
            "성향": ["안정형", "중립형", "적극형"],
            "채권·예금": ["80%", "50%", "30%"],
            "주식·펀드": ["20%", "50%", "70%"],
            "기대수익률(예시)": ["2~3%", "4~5%", "6~7%"],
        }), width="stretch", hide_index=True)
        st.caption("시뮬레이션은 가정 기반 예시입니다. 실제 수익률·세액공제는 상품·소득·세법에 따라 달라집니다.")
    with t[1]:
        _render_product_compare("irpp")
    with t[2]:
        _render_bulk_consult(user, "irpc", ["IRP 가입/세제", "DC형 도입/전환"])
    with t[3]:
        _render_consultants("irpcst")


# 금융기관 상품 비교 · 파트너 컨설턴트 (설계안 · 예시 데이터)
_DC_PRODUCTS = [
    {"금융기관": "OO은행", "유형": "원리금보장", "상품명": "정기예금형 DC", "최근수익률(%)": 3.4,
     "총보수(%)": 0.30, "위험등급": "낮음", "최소가입": "제한없음", "특징": "예금자보호·안정"},
    {"금융기관": "OO증권", "유형": "실적배당", "상품명": "TDF2045", "최근수익률(%)": 8.7,
     "총보수(%)": 0.65, "위험등급": "다소높음", "최소가입": "1만원", "특징": "생애주기 자동배분"},
    {"금융기관": "OO생명", "유형": "원리금보장", "상품명": "이율보증형(GIC)", "최근수익률(%)": 3.1,
     "총보수(%)": 0.25, "위험등급": "낮음", "최소가입": "제한없음", "특징": "금리보증"},
    {"금융기관": "OO자산운용", "유형": "실적배당", "상품명": "채권혼합형", "최근수익률(%)": 5.2,
     "총보수(%)": 0.45, "위험등급": "보통", "최소가입": "1만원", "특징": "채권중심 안정추구"},
    {"금융기관": "OO증권", "유형": "실적배당", "상품명": "글로벌주식형", "최근수익률(%)": 11.3,
     "총보수(%)": 0.80, "위험등급": "높음", "최소가입": "1만원", "특징": "성장·고변동"},
    {"금융기관": "OO은행", "유형": "실적배당", "상품명": "인덱스혼합형", "최근수익률(%)": 6.1,
     "총보수(%)": 0.40, "위험등급": "보통", "최소가입": "1만원", "특징": "저비용 지수추종"},
]
_PARTNER_CONSULTANTS = [
    {"이름": "김OO", "소속": "위키소프트 연금컨설팅본부", "전문분야": "DB 운영·ALM",
     "경력": "계리사 12년 · DB 재정검증 200건+", "전화": "02-000-1234",
     "이메일": "db.kim@wiki.example", "평점": "★★★★★"},
    {"이름": "이OO", "소속": "OO증권 퇴직연금센터", "전문분야": "DC·TDF 운용",
     "경력": "펀드매니저 9년 · DC 자산배분", "전화": "02-000-2345",
     "이메일": "dc.lee@wiki.example", "평점": "★★★★☆"},
    {"이름": "박OO", "소속": "OO생명 기업연금팀", "전문분야": "IRP·세제",
     "경력": "FP 8년 · IRP 세액공제 상담 1,000건+", "전화": "02-000-3456",
     "이메일": "irp.park@wiki.example", "평점": "★★★★★"},
]


def _render_product_compare(key_prefix):
    """금융기관 상품 비교 — DB·DC 컨설팅 화면에서 공용."""
    df = pd.DataFrame(_DC_PRODUCTS)
    f = st.columns(3)
    kind = f[0].selectbox("유형", ["전체", "원리금보장", "실적배당"], key=f"{key_prefix}_kind")
    insts = sorted(df["금융기관"].unique().tolist())
    pick_inst = f[1].multiselect("금융기관", insts, default=insts, key=f"{key_prefix}_inst")
    sort_by = f[2].selectbox("정렬", ["최근수익률(%)", "총보수(%)", "위험등급"], key=f"{key_prefix}_sort")
    view = df[df["금융기관"].isin(pick_inst)]
    if kind != "전체":
        view = view[view["유형"] == kind]
    view = view.sort_values(sort_by, ascending=(sort_by != "최근수익률(%)"))
    st.dataframe(view, width="stretch", hide_index=True)
    st.caption("수익률은 예시입니다. 실제 상품·수익률·보수는 시점·기관 공시에 따릅니다. "
               "원리금보장은 예금자보호, 실적배당은 원금손실 가능.")
    if not view.empty:
        st.markdown("##### 최근수익률 비교")
        st.bar_chart(view.set_index("상품명")["최근수익률(%)"])


def _render_bulk_consult(user, key_prefix, default_types):
    """일괄 상담 신청 폼 — DB·DC 컨설팅 화면에서 공용(default_types로 기본 상담유형 지정)."""
    st.caption("관심 상품·상담 유형을 골라 한 번에 상담을 신청합니다. 신청 내용은 요약으로 확인·다운로드됩니다.")
    prod_names = [f"{p['금융기관']} · {p['상품명']}" for p in _DC_PRODUCTS]
    all_types = ["DB형 운영 컨설팅", "DC형 도입/전환", "IRP 가입/세제", "자산운용(ALM)", "재정검증"]
    with st.form(f"{key_prefix}_form"):
        ctype = st.multiselect("상담 유형", all_types, default=default_types, key=f"{key_prefix}_type")
        picks = st.multiselect("관심 금융기관·상품", prod_names, key=f"{key_prefix}_prod")
        cc = st.columns(3)
        name = cc[0].text_input("담당자/성명", key=f"{key_prefix}_name")
        phone = cc[1].text_input("연락처", key=f"{key_prefix}_phone")
        when = cc[2].text_input("희망 상담시간", placeholder="예: 평일 오후", key=f"{key_prefix}_when")
        memo = st.text_area("문의 내용", key=f"{key_prefix}_memo", height=80)
        submitted = st.form_submit_button("📨 상담 신청", type="primary")
    if submitted:
        if not ctype and not picks:
            st.warning("상담 유형 또는 관심 상품을 하나 이상 선택하세요.")
        else:
            summary = (f"[상담 신청]\n회사: {user['company_name']}\n"
                       f"상담유형: {', '.join(ctype) or '-'}\n"
                       f"관심상품: {', '.join(picks) or '-'}\n"
                       f"담당자: {name or '-'} / 연락처: {phone or '-'} / 희망시간: {when or '-'}\n"
                       f"문의: {memo or '-'}")
            st.success("✅ 상담 신청이 접수되었습니다. 담당 컨설턴트가 연락드립니다. (설계안 — 실제 접수 연동 예정)")
            st.code(summary)
            st.download_button("📥 신청 내용 내려받기", summary.encode("utf-8"),
                               file_name=f"{user['company_name']}_상담신청.txt", key=f"{key_prefix}_dl")


def _render_consultants(key_prefix):
    """파트너 컨설턴트 이력·연락처 — DB·DC 컨설팅 화면에서 공용."""
    st.caption("분야별 파트너 컨설턴트입니다. 연락처로 직접 상담하거나 '상담 신청'을 이용하세요.")
    for i, cst in enumerate(_PARTNER_CONSULTANTS):
        with st.container(border=True):
            a, b = st.columns([3, 1])
            a.markdown(f"**{cst['이름']}**  ·  {cst['소속']}  {cst['평점']}")
            a.caption(f"전문분야: {cst['전문분야']}  |  {cst['경력']}")
            a.markdown(f"📞 {cst['전화']}   ✉️ {cst['이메일']}")
            if b.button("상담 요청", key=f"{key_prefix}_cst_{i}", width="stretch"):
                st.success(f"{cst['이름']} 컨설턴트에게 상담 요청이 전달되었습니다. (설계안)")


def _client_main(user):
    """IFRS 1019 퇴직급여 부채 관리 — 현황조회 · 신청 · 기본정보(기존 기업 전용 화면)."""
    # 계리사가 기업검토를 요청한(확인 대기) 산출건 알림 배너
    _rev = [s for s in store.list_submissions(DB_PATH, company_id=user["company_id"])
            if s["status"] == "client_review" and not s.get("client_confirmed_at")]
    if _rev:
        _names = ", ".join(f"[{_apply_no(s)}] {s['valuation_date']}" for s in _rev)
        st.warning(f"🔎 **계리사가 검토를 요청한 산출건이 {len(_rev)}건 있습니다** — {_names}\n\n"
                   "아래 **현황조회**에서 주황색 **‘🔎 기업검토요청 · 확인’** 을 눌러 보고서를 확인한 뒤 "
                   "**검토완료**를 눌러주세요. (검토완료해야 계리사가 최종 보고서를 확정합니다.)")
    _client_past_reports(user)      # 1. 현황조회 (리스트에서 '수정' 클릭 시 편집모드 진입)
    st.divider()
    # 리스트에서 특정 건 '수정'을 누르면 편집화면을 열고, 신청화면은 숨긴다(폼 충돌 방지).
    editing = st.session_state.get("client_detail_sid")
    if editing and store.get_submission(DB_PATH, editing):
        _client_edit_screen(user, editing)
    else:
        st.session_state.pop("client_detail_sid", None)
        _client_apply(user)         # 2. 확정급여제도 부채 계산 신청
    st.divider()
    with st.expander("🏢 기업 기본 정보 (담당자·연락처·결산월·주소)", expanded=False):
        _client_company_info(user)      # 기업 기본 정보 (접이식, 화면 절반↓)


def _client_edit_screen(user, sid):
    """현황조회에서 '수정' 클릭 → 기존 입력화면(제도·운영현황·명부·기타장기)으로 수정 + 수정완료."""
    sub = store.get_submission(DB_PATH, sid)
    st.subheader(f"✏️ 신청 수정 — [신청번호 {_apply_no(sub)}] {sub['company_name']} · "
                 f"산출기준일 {sub['valuation_date']}")

    # 접수 후(수정 불가) 또는 기업검토요청 단계면 편집 대신 상세(검토확인/메일)만
    if not store.can_client_modify(sub["status"]):
        _client_detail(user, sid)
        if st.button("↩ 목록으로", key=f"editback_{sid}"):
            st.session_state.pop("client_detail_sid", None)
            st.session_state.pop("client_sel_sid", None)
            st.rerun()
        return

    st.caption("아래에서 수정하세요. 각 항목은 **저장** 버튼으로 저장되고, 명부는 다시 올리면 갱신됩니다. "
               "다 마치면 맨 아래 **수정 완료**를 누르세요.")
    val_date = dt.date.fromisoformat(sub["valuation_date"])
    with st.expander("① 🏢 제도입력 (퇴직금 제도 정보)", expanded=True):
        _client_plan_info_form(user)
    with st.expander("② 📋 명부 수정 (재업로드하면 이 건이 갱신)", expanded=True):
        _client_census_form(user, val_date, sub["purpose"] or PURPOSES[0], edit_sid=sid)
    with st.expander("③ 💰 사외적립자산", expanded=False):
        _client_operation_status(user)
    with st.expander("④ 🎖 기타장기", expanded=False):
        _client_other_lt(user)
    with st.expander("⑤ 📑 명부확인용 요약표 (재직자명부 등록 후)", expanded=False):
        _client_census_summary(user)
    with st.expander("⑥ 📈 경험기초율 산출데이터  ·  300인 이상 기업 중 자체 경험율 사용 기업만 해당",
                     expanded=False):
        _client_experience_data(user)

    st.divider()
    b1, b2, _ = st.columns([1.3, 1.3, 4])
    if b1.button("✅ 수정 완료", type="primary", key=f"editdone_{sid}", width="stretch"):
        st.session_state.pop("client_detail_sid", None)
        st.session_state.pop("client_sel_sid", None)
        st.success("✅ 수정이 반영되었습니다.")
        st.rerun()
    if b2.button("↩ 취소(닫기)", key=f"editcancel_{sid}", width="stretch"):
        st.session_state.pop("client_detail_sid", None)
        st.rerun()


# ── 1. 지난 보고서 조회 ─────────────────────────────────────────────────────
def _client_category(status: str) -> str:
    """기업 관점 구분: 신청 / 진행 / 검토 / 완료 / 취소."""
    if status in ("needs_fix", "validated", "submitted"):
        return "신청"
    if status in ("accepted", "on_hold", "calculated"):
        return "진행"
    if status == "client_review":
        return "검토"
    if status == "reported":
        return "완료"
    if status == "cancelled":
        return "취소"
    return "기타"


def _submission_progress(sub) -> str:
    """진행상태 + 단계별 날짜: 신청 / 접수완료 / 계산완료 / 기업검토요청 / 기업확인완료 / 최종보고서완료."""
    s = sub["status"]

    def _d(f):
        v = sub.get(f)
        return f" ({v[:10]})" if v else ""

    if s == "reported":
        return "최종보고서완료" + _d("reported_at")
    if s == "client_review":
        return ("기업확인완료" + _d("client_confirmed_at")) if sub.get("client_confirmed_at") \
               else ("기업검토요청" + _d("review_requested_at"))
    if s == "calculated":
        return "계산완료" + _d("calculated_at")
    if s == "accepted":
        return "접수완료" + _d("accepted_at")
    if s == "on_hold":
        return "보류중"
    return "신청"


def _report_file_for(sub):
    res = store.latest_result(DB_PATH, sub["id"])
    if not res:
        return None
    base = Path(res["xlsx_path"]).parent
    for name in ("dbo_report.pptx", "dbo_report.xlsx"):
        f = base / name
        if f.exists():
            return f
    return None


_LIST_WIDTHS = [0.85, 0.55, 1.4, 1.2, 1.0, 1.4, 2.0, 1.4]
_LIST_HEADERS = ["신청번호", "선택", "기업명", "산출기준일", "신청일", "산출목적", "진행상태", "다운로드"]
_APPLY_NO_BASE = 1010   # 신청번호 = base + submission.id (1번 → 1011)


def _apply_no(sub) -> int:
    return _APPLY_NO_BASE + int(sub["id"])


def _render_submission_list(user, subs, role, pending=None, show_edit=True):
    """기업/계리사 공통 신청 목록. 선택(동그라미)→수정/삭제. 반환: 수정(상세)할 sid 또는 None.

    show_edit=False면 '수정' 버튼을 숨긴다(계리사는 선택만으로 작업화면이 열림).
    """
    pending = pending or {}
    sel_key, detail_key, del_key = f"{role}_sel_sid", f"{role}_detail_sid", f"{role}_del_sid"
    ids = [s["id"] for s in subs]
    sel = st.session_state.get(sel_key)
    sel = sel if sel in ids else None

    _hr = "<hr style='margin:1px 0;border:none;border-top:1px solid #d5dae0'>"
    hdr = st.columns(_LIST_WIDTHS)
    for c, t in zip(hdr, _LIST_HEADERS):
        c.markdown(f"<div style='font-weight:700'>{t}</div>", unsafe_allow_html=True)
    st.markdown("<hr style='margin:1px 0;border:none;border-top:2px solid #1F4E79'>",
                unsafe_allow_html=True)
    for s in subs:
        is_sel = (s["id"] == sel)
        cols = st.columns(_LIST_WIDTHS, vertical_alignment="center")
        cols[0].markdown(f"**{_apply_no(s)}**")
        if cols[1].button("🔘" if is_sel else "⚪", key=f"{role}_sel_{s['id']}", help="선택"):
            st.session_state[sel_key] = None if is_sel else s["id"]
            st.session_state.pop(detail_key, None)
            st.rerun()
        nm = (s["company_name"] or "-")[:6]
        cols[2].markdown(f"**{nm}**" if is_sel else nm)
        cols[3].write(s["valuation_date"])
        cols[4].write((s["created"] or "")[:10])
        cols[5].caption(s["purpose"] or "IFRS-1019")
        badge = f"  💬{pending.get(s['company_id'], 0)}" if pending.get(s["company_id"]) else ""
        prog = _submission_progress(s)
        awaiting_review = (s["status"] == "client_review" and not s.get("client_confirmed_at"))
        if role == "client" and awaiting_review:
            # 기업검토요청 → 기업이 눌러서 보고서 확인·검토완료로 진입
            if cols[6].button(f"🔎 {prog} · 확인", key=f"{role}_rev_{s['id']}",
                              type="primary", width="stretch",
                              help="보고서를 확인하고 검토완료를 누르세요"):
                st.session_state[detail_key] = s["id"]
                st.session_state[sel_key] = s["id"]
                st.rerun()
        elif awaiting_review:
            cols[6].markdown(
                f"<span style='background:#fdecea;color:#c0392b;font-weight:700;"
                f"padding:1px 7px;border-radius:6px'>🔎 {prog}</span>{badge}",
                unsafe_allow_html=True)
        else:
            cols[6].write(prog + badge)
        # 다운로드: 보고서/명부를 좌우로 작게
        d1, d2 = cols[7].columns(2)
        rf = _report_file_for(s)
        if rf and (role == "actuary" or s["status"] in ("reported", "client_review")):
            d1.download_button("📑", rf.read_bytes(), help="보고서 내려받기",
                               file_name=f"{s['company_name']}_{s['valuation_date']}_보고서{rf.suffix}",
                               key=f"{role}_rep_{s['id']}")
        mp = Path(s["stored_path"])
        if mp.exists():
            d2.download_button("📄", mp.read_bytes(), help="명부 내려받기", file_name=mp.name,
                               key=f"{role}_men_{s['id']}")
        st.markdown(_hr, unsafe_allow_html=True)

    # 선택 시: 수정/삭제 액션 (선택된 건에 대해)
    if sel is not None:
        ssub = next(s for s in subs if s["id"] == sel)
        can_edit = store.can_client_modify(ssub["status"]) if role == "client" else True
        can_del = (store.can_client_modify(ssub["status"]) if role == "client"
                   else store.can_actuary_delete(ssub["status"]))
        with st.container(border=True):
            st.markdown(f"👉 선택됨: **[{_apply_no(ssub)}] {(ssub['company_name'] or '-')}** · "
                        f"{ssub['valuation_date']} — {_submission_progress(ssub)}")
            a1, a2, a3 = st.columns([1.2, 1.2, 4])
            if show_edit and a1.button("✏️ 수정", key=f"{role}_edit_btn", type="primary",
                                       disabled=not can_edit, width="stretch",
                                       help=None if can_edit else "계리사 접수 후에는 기업이 수정할 수 없습니다."):
                st.session_state[detail_key] = sel
                st.rerun()
            if a2.button("🗑 삭제", key=f"{role}_del_btn", disabled=not can_del, width="stretch",
                         help=None if can_del else "보고완료 건은 삭제할 수 없습니다."):
                st.session_state[del_key] = sel
                st.rerun()
            if show_edit and not can_edit and not can_del:
                a3.caption("이 단계에서는 기업이 수정·삭제할 수 없습니다(계리사 접수 후).")

    # 삭제 확인
    del_sid = st.session_state.get(del_key)
    if del_sid in ids:
        ds = next(s for s in subs if s["id"] == del_sid)
        st.warning(f"⚠️ **{ds['company_name']} · {ds['valuation_date']}** 신청건을 정말 삭제하시겠습니까? "
                   "(명부·산출결과 포함, 되돌릴 수 없습니다)")
        d1, d2, _ = st.columns([1, 1, 4])
        if d1.button("✅ 확인 삭제", type="primary", key=f"{role}_del_yes"):
            perm = (store.can_client_modify(ds["status"]) if role == "client"
                    else store.can_actuary_delete(ds["status"]))
            if perm:
                try:
                    Path(ds["stored_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
                store.delete_submission(DB_PATH, del_sid)
                store.log_action(DB_PATH, user["id"], "delete", now(), f"submission#{del_sid}")
                for k in (sel_key, detail_key):
                    if st.session_state.get(k) == del_sid:
                        st.session_state.pop(k, None)
            st.session_state.pop(del_key, None)
            st.rerun()
        if d2.button("취소", key=f"{role}_del_no"):
            st.session_state.pop(del_key, None)
            st.rerun()

    dsid = st.session_state.get(detail_key)
    return dsid if dsid in ids else None


def _client_past_reports(user):
    from collections import Counter
    st.subheader("1. 현황조회")
    subs = store.list_submissions(DB_PATH, company_id=user["company_id"])
    if not subs:
        st.info("아직 신청 내역이 없습니다. 아래 '2. 확정급여제도 부채 계산 신청'에서 진행하세요.")
        _render_qa_panel(user["company_id"], "client", user["id"])
        return

    cc = Counter(_client_category(s["status"]) for s in subs)
    opts = ["전체", "신청", "진행", "검토", "완료"]

    def _fl(x):
        return f"전체 ({len(subs)})" if x == "전체" else f"{x} ({cc.get(x, 0)})"

    pick = st.radio("구분", opts, horizontal=True, format_func=_fl, key="client_status_pick")
    rows = [s for s in subs if pick == "전체" or _client_category(s["status"]) == pick]
    if not rows:
        st.info("해당 구분의 신청 건이 없습니다.")
    else:
        # '수정' 클릭 시 client_detail_sid가 설정되고, page_client가 편집화면을 연다.
        _render_submission_list(user, rows, "client")

    st.divider()
    _render_qa_panel(user["company_id"], "client", user["id"])

    # 감사 대응 Q&A(보고서 설명) — 기업검토요청 단계부터 보고서 내용 설명 제공
    _explain = [s for s in subs if s["status"] in ("client_review", "reported")
                and store.latest_result(DB_PATH, s["id"])]
    if _explain:
        st.divider()
        st.markdown("### 📋 보고서 설명 (감사 대응 Q&A)")
        st.caption("계리사가 작성한 보고서 내용에 대한 표준 설명·근거입니다. 검토·감사·경영진 보고 시 활용하세요.")
        if len(_explain) == 1:
            _esub = _explain[0]
        else:
            _eopt = {s["id"]: f"[{_apply_no(s)}] {s['valuation_date']} · {store.stage_of(s['status'])}"
                     for s in _explain}
            _epick = st.selectbox("산출건 선택", list(_eopt), format_func=lambda i: _eopt[i],
                                  key="client_aqa_pick")
            _esub = next(s for s in _explain if s["id"] == _epick)
        _render_audit_qa(_esub, f"c_{_esub['id']}", expanded=True)


def _client_detail(user, sid):
    """현황조회에서 '수정' 클릭 시 나오는 해당 건 상세 — 검토확인·명부 재업로드·메일."""
    s = store.get_submission(DB_PATH, sid)
    if not s:
        return
    with st.container(border=True):
        st.markdown(f"#### ✏️ {s['company_name']} · {s['valuation_date']}  —  {_submission_progress(s)}")

        if s["status"] == "client_review":
            if s.get("client_confirmed_at"):
                st.success(f"검토 확인 완료 ({(s['client_confirmed_at'] or '')[:16]}) — "
                           "계리사의 최종 보고완료를 기다립니다.")
            else:
                st.warning("계리사가 **기업검토를 요청**했습니다. 위 목록의 **📑보고서**를 내려받아 확인한 뒤 "
                           "아래 **검토완료**를 눌러주세요.")
                if st.button("✅ 검토완료 (보고서 내용 확인)", type="primary", key=f"cdrev_{sid}"):
                    store.mark_client_confirmed(DB_PATH, sid, now())
                    store.log_action(DB_PATH, user["id"], "client_confirm", now(), f"submission#{sid}")
                    st.rerun()
        elif store.can_client_modify(s["status"]):
            st.caption("명부를 다시 올리면 **이 건이 갱신**됩니다(같은 산출기준일, 새 건 안 생김).")
            up = st.file_uploader("📋 명부 재업로드 (xlsx / csv)", type=["xlsx", "csv"],
                                  key=f"reup_{sid}")
            if up is not None:
                raw = bytes(up.getbuffer())
                sig = f"reup|{sid}|{up.name}|{len(raw)}"
                if st.session_state.get(f"reup_done_{sid}") != sig:
                    FILES_DIR.mkdir(parents=True, exist_ok=True)
                    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    saved = FILES_DIR / f"c{user['company_id']}_active_{stamp}_{up.name}"
                    dropped, err = _save_census_file(raw, up.name, saved)
                    if err:
                        st.error(f"파일을 읽을 수 없습니다: {err}")
                    else:
                        vdate = dt.date.fromisoformat(s["valuation_date"])
                        recs, rep, _ = load_census(saved, column_map=COLMAP)
                        validate_census(recs, vdate, rep)
                        run_smart_checks(recs, vdate, rep)
                        st_new = "needs_fix" if rep.has_errors else "validated"
                        store.update_submission_file(DB_PATH, sid, up.name, str(saved),
                                                     rep.n_records, len(rep.errors),
                                                     len(rep.warnings), now())
                        store.update_submission_status(DB_PATH, sid, st_new, now())
                        store.log_action(DB_PATH, user["id"], "reupload", now(), f"submission#{sid}")
                        st.session_state[f"reup_done_{sid}"] = sig
                        st.success(f"명부를 교체했습니다 — {rep.n_records}명 · 오류 {len(rep.errors)} · "
                                   f"경고 {len(rep.warnings)}. 신청 단계로 갱신되었습니다.")
                        if dropped:
                            st.info(f"🔒 개인정보 컬럼 자동삭제: {', '.join(dropped)}")
        else:
            st.caption("접수 이후에는 계리사가 처리합니다. 진행상태를 확인하세요.")

        # 확정 보고서 이메일 받기
        rf = _report_file_for(s)
        if s["status"] == "reported" and rf:
            with st.expander("📧 보고서 이메일로 받기", expanded=False):
                sales = store.get_sales(DB_PATH, user["company_id"]) or {}
                to = st.text_input("받는 사람 이메일", value=sales.get("contact_email") or "",
                                   key=f"cmailto_{sid}", placeholder="예: manager@company.com")
                if mailer.valid_email(to):
                    y = s["valuation_date"][:4]
                    subject = f"[{user['company_name']}] {y}년 K-IFRS 제1019호 계리평가보고서"
                    body = (f"{user['company_name']} 담당자님,\n\n{y}년 계리평가보고서를 첨부드립니다."
                            "\n\n감사합니다.")
                    eml = mailer.build_eml("no-reply@dbo-platform.local", [to], subject, body,
                                           attachments=[rf])
                    st.download_button("📥 이메일 초안(.eml) 만들기", eml,
                                       file_name=f"{user['company_name']}_보고서메일.eml",
                                       key=f"ceml_{sid}")
                    cfg = mailer.smtp_config_from_env()
                    if cfg and st.button("📧 지금 바로 받기(전송)", key=f"csend_{sid}"):
                        ok, msg = mailer.send_smtp(cfg, [to], subject, body, attachments=[rf])
                        st.success(msg) if ok else st.error(msg)


# ── 2. 확정급여제도 부채 계산 신청 ──────────────────────────────────────────
def _client_apply(user):
    st.subheader("2. 확정급여제도 부채 계산 신청")
    c1, c2 = st.columns(2)
    val_date = c1.date_input("산출기준일", dt.date(dt.date.today().year, 12, 31), key="apply_vdate")
    purpose = c2.selectbox("산출목적", PURPOSES, key="apply_purpose")
    st.caption("① 제도입력 → ② 명부등록 → ③ 사외적립자산 → ④ 기타장기 → ⑤ 명부확인용 요약표 → "
               "⑥ 경험기초율 → ⑦ 지난보고서 순으로 진행 후 **신청하기**를 누르세요.")

    with st.expander("① 🏢 제도입력 (퇴직금 제도 정보)", expanded=False):
        _client_plan_info_form(user)
    with st.expander("② 📋 명부등록", expanded=True):
        _client_census_form(user, val_date, purpose)
    with st.expander("③ 💰 사외적립자산", expanded=False):
        _client_operation_status(user)
    with st.expander("④ 🎖 기타장기 (장기근속 포상 등 기타장기종업원급여)", expanded=False):
        _client_other_lt(user)
    with st.expander("⑤ 📑 명부확인용 요약표 (재직자명부 등록 후)", expanded=False):
        _client_census_summary(user)
    with st.expander("⑥ 📈 경험기초율 산출데이터  ·  300인 이상 기업 중 자체 경험율 사용 기업만 해당",
                     expanded=False):
        _client_experience_data(user)
    with st.expander("⑦ 📎 지난보고서 등록 (전 계리법인 자료)", expanded=False):
        _client_prior_form(user)

    st.divider()
    subs = store.list_submissions(DB_PATH, company_id=user["company_id"])
    pending = [s for s in subs if s["status"] in ("validated", "needs_fix")]
    if pending:
        latest = pending[0]
        note = f"제출 대기 명부: {latest['n_records']}명 · 오류 {latest['n_errors']} · 경고 {latest['n_warnings']}"
        if latest["n_errors"]:
            note += "  (오류가 있어도 이대로 신청 가능)"
        st.caption(note)
        if st.button("🟨 신청하기", type="primary", width="stretch"):
            store.set_submission_meta(DB_PATH, latest["id"], purpose=purpose, calculator=CALCULATOR_FIRM)
            store.update_submission_status(DB_PATH, latest["id"], "submitted", now())
            store.log_action(DB_PATH, user["id"], "submit", now(), f"submission#{latest['id']}")
            st.session_state["client_last_submitted"] = latest["id"]
            st.success("✅ 신청이 완료되었습니다. 위 **현황조회** 리스트에서 진행상태를 확인하세요.")
            st.rerun()
    else:
        # 이미 신청(제출)된 건만 있고 새로 올린 명부가 없으면 중복 신청 방지
        already = [s for s in subs if s["status"] == "submitted"]
        if already:
            st.button("🟨 신청하기", disabled=True, width="stretch")
            st.caption("ℹ️ 변경사항이 없습니다 — 이미 신청된 건이 있습니다. "
                       "수정하려면 명부를 다시 업로드하거나 현황조회에서 수정/삭제하세요.")
        else:
            st.button("🟨 신청하기", disabled=True, width="stretch",
                      help="먼저 '명부등록'에서 명부 파일을 업로드하세요.")


def _client_plan_info_form(user):
    pi = store.get_plan_info(DB_PATH, user["company_id"]) or {}
    try:
        d = json.loads(pi["detail_json"]) if pi.get("detail_json") else {}
    except Exception:
        d = {}
    this_year = dt.date.today().year
    years = [str(y) for y in range(this_year - 5, this_year)]   # 과거 5년

    with st.form("plan_info"):
        st.markdown("**① 제도 일반**")
        a, b = st.columns(2)
        _pt = ["미설정", "DB(확정급여)", "DC(확정기여)", "DB+DC 병행"]
        plan_type = a.selectbox("제도 유형", _pt, index=_idx(_pt, pi.get("plan_type")))
        established = b.text_input("제도 설정일", pi.get("established_date") or "", placeholder="예: 2015-01-01")
        benefit_rule = a.selectbox("퇴직금 규정", BENEFIT_RULES,
                                   index=_idx(BENEFIT_RULES, pi.get("benefit_rule")))
        _sb = ["평균임금", "통상임금", "기타"]
        salary_basis = b.selectbox("급여 산정 기준", _sb, index=_idx(_sb, pi.get("salary_basis")))
        benefit_rule_desc = st.text_area(
            "누진제/기타 제도 서술 (누진제·기타 선택 시 지급률·배율 등 구두 기술)",
            value=d.get("benefit_rule_desc", ""),
            placeholder="예: 근속 10년 이상 지급률 1.5배, 20년 이상 2.0배 …")

        st.divider()
        st.markdown("**② 정년·근무기간**")
        c, e = st.columns(2)
        retirement_age = c.number_input("정년 퇴직 연령(만)", value=int(pi.get("retirement_age") or 60), step=1)
        exec_ret = e.number_input("임원 퇴직 연령(별도 운영 시, 0=없음)",
                                  value=int(d.get("exec_retirement_age") or 0), step=1)
        wage_peak = c.checkbox("임금피크제 적용", value=bool(d.get("wage_peak_applied")))
        wage_peak_age = e.number_input("임금피크제 시작연령(만)",
                                       value=int(d.get("wage_peak_age") or 0), step=1)
        under1yr = c.selectbox("1년 미만 근무 처리방법", UNDER1YR_METHODS,
                               index=_idx(UNDER1YR_METHODS, d.get("under1yr_method")))
        normal_cost_ret = e.checkbox("정년연령 해당자 당기근무원가 산출",
                                     value=bool(d.get("normal_cost_at_retirement")))
        rate_adjust = st.text_input("지급률 가감 내용 (군복무·포상·장기근속 +, 휴직·징계 − 등)",
                                    value=d.get("rate_adjust_desc", ""))
        interim_allowed = c.checkbox("중간정산 허용", value=bool(pi.get("interim_allowed")))
        interim_cycle = e.text_input("중간정산 주기", pi.get("interim_cycle") or "")

        st.divider()
        st.markdown("**③ 할인율 및 사외적립비율**")

        # ── 할인율 산출 기준 (회사채 신용등급, 기타 선택 시 직접 입력) ──
        _saved_db = d.get("discount_basis") or "AA+"
        if _saved_db == "회사채AA+":       # 레거시 저장값 보정
            _saved_db = "AA+"
        discount_basis = st.selectbox(
            "할인율 산출 기준 (회사채 신용등급)", DISCOUNT_BASES,
            index=_idx(DISCOUNT_BASES, _saved_db))
        discount_other = st.text_input(
            "└ 기타 선택 시 직접 입력 (예: 국공채, BBB+ 등)",
            value=d.get("discount_basis_other", ""),
            disabled=(discount_basis != "기타"))
        st.caption(DISCOUNT_RATE_NOTE)

        st.markdown("")   # 간격
        # ── 사외적립자산 적립비율(가입비율) (기타 선택 시 직접 입력) ──
        _cur_ratio = pi.get("funding_ratio")
        _cur_label = None
        if _cur_ratio not in (None, "", 0, 0.0):
            _cur_label = f"{int(round(float(_cur_ratio)))}%"
        if _cur_label in FUNDING_RATIO_OPTIONS:
            _ratio_idx = FUNDING_RATIO_OPTIONS.index(_cur_label)   # 저장값이 목록에 있음
        elif _cur_ratio not in (None, "", 0, 0.0):
            _ratio_idx = FUNDING_RATIO_OPTIONS.index("기타")        # 목록 밖 값 → 기타
        else:
            _ratio_idx = FUNDING_RATIO_OPTIONS.index("100%")       # 미설정 → 기본 100%
        funding_ratio_sel = st.selectbox(
            "사외적립자산 적립비율(가입비율)", FUNDING_RATIO_OPTIONS, index=_ratio_idx)
        _ratio_other_default = ""
        if _cur_label not in FUNDING_RATIO_OPTIONS and _cur_ratio not in (None, "", 0, 0.0):
            _ratio_other_default = f"{float(_cur_ratio):g}"
        funding_ratio_other = st.text_input(
            "└ 기타 선택 시 적립비율(%) 직접 입력 (예: 65)",
            value=_ratio_other_default,
            disabled=(funding_ratio_sel != "기타"))
        st.caption("※ 평가일로부터 1년 후(다음 회계년도 말) 회사가 계획하는 사외자산 "
                   "적립비율을 선택하여 주십시오. (기타의 경우 별도 기입)")

        st.divider()
        st.markdown("**④ 임금인상률(Base-up)**")
        _wsys = ["호봉제", "연봉제"]
        wage_system = st.selectbox("임금체계", _wsys, index=_idx(_wsys, d.get("salary_system") or "연봉제"),
                                   help="호봉제: 근무기간에 따라 호봉 상승 / 연봉제: 성과에 따라 급여 결정")
        proposed = st.number_input(
            "고객사 제안 임금인상율(%) — 평가 적용 (호봉 승급에 의한 인상 제외)",
            value=float(d.get("baseup_proposed") or 0.0), step=0.1, format="%.2f")
        st.caption("과거 5년치 임금인상율(%) — 고객사 제안이 없으면 아래 5년 평균을 적용합니다.")
        bu = d.get("baseup", {})
        bcols = st.columns(len(years))
        baseup = {}
        for col, yr in zip(bcols, years):
            baseup[yr] = col.number_input(f"{yr}년", value=float(bu.get(yr) or 0.0), step=0.1,
                                          key=f"bu_{yr}", format="%.2f")
        _nz = [v for v in baseup.values() if v]
        _avg = sum(_nz) / len(_nz) if _nz else 0.0
        st.caption(f"📊 과거 5년 평균: **{_avg:.2f}%**")
        st.caption("직군별(제도구분별)로 인상률이 다르면 아래 표에 연도·제도1~6 입력 (제도구분은 재직자명부 '제도구분'열과 매칭)")
        grp_saved = d.get("baseup_by_group")
        if grp_saved:
            grp_df = pd.DataFrame(grp_saved)
        else:
            grp_df = pd.DataFrame([{"연도": yr, "제도1": "", "제도2": "", "제도3": "",
                                    "제도4": "", "제도5": "", "제도6": ""} for yr in years])
        grp_edit = st.data_editor(grp_df, num_rows="dynamic", width="stretch", key="baseup_grp")

        notes = st.text_area("특이사항", pi.get("notes") or "")

        if st.form_submit_button("💾 제도 정보 저장", type="primary"):
            # 적립비율: 드롭다운 값 또는 '기타' 직접입력 → float(%)
            if funding_ratio_sel == "기타":
                try:
                    funding_ratio = float(str(funding_ratio_other).replace("%", "").strip() or 0.0)
                except ValueError:
                    funding_ratio = 0.0
            else:
                funding_ratio = float(funding_ratio_sel.replace("%", ""))
            detail = {
                "benefit_rule_desc": benefit_rule_desc,
                "exec_retirement_age": int(exec_ret), "wage_peak_applied": bool(wage_peak),
                "wage_peak_age": int(wage_peak_age), "under1yr_method": under1yr,
                "normal_cost_at_retirement": bool(normal_cost_ret), "rate_adjust_desc": rate_adjust,
                "discount_basis": discount_basis, "discount_basis_other": discount_other,
                "salary_system": wage_system, "salary_hobong": (wage_system == "호봉제"),
                "baseup_proposed": float(proposed), "baseup": baseup,
                "baseup_by_group": grp_edit.to_dict("records"),
            }
            store.save_plan_info(DB_PATH, user["company_id"], {
                "plan_type": plan_type, "established_date": established, "benefit_rule": benefit_rule,
                "interim_allowed": int(interim_allowed), "interim_cycle": interim_cycle,
                "retirement_age": int(retirement_age), "salary_basis": salary_basis,
                "funding_ratio": float(funding_ratio), "notes": notes,
                "detail_json": json.dumps(detail, ensure_ascii=False),
            }, user["id"], now())
            store.log_action(DB_PATH, user["id"], "plan_info", now())
            st.success("제도 정보를 저장했습니다.")

    # 누진제/기타 관련 서류 업로드 (폼 밖 — 즉시 저장)
    if pi.get("benefit_rule") in ("누진제", "기타"):
        st.markdown("**📎 누진제/기타 제도 관련 서류 업로드**")
        up = st.file_uploader("규정·산식 서류 (pdf/이미지/xlsx)", type=["pdf", "jpg", "jpeg", "png", "xlsx"],
                              key="prog_rule_up")
        if up is not None and st.button("서류 저장", key="prog_rule_save"):
            d2 = FILES_DIR / "plan_rule" / f"c{user['company_id']}"
            d2.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            p = d2 / f"{stamp}_{up.name}"
            p.write_bytes(up.getbuffer())
            store.add_aux_census(DB_PATH, user["company_id"], "누진제규정서류", up.name, str(p),
                                 user["id"], now())
            st.success("서류를 저장했습니다.")
        for f in store.list_aux_census(DB_PATH, user["company_id"], "누진제규정서류"):
            st.caption(f"📄 {f['filename']}")


def _latest_active_census_records(company_id: int):
    """회사의 최신 재직자명부(제출건) 레코드를 로드해 반환. 없으면 None."""
    try:
        subs = store.list_submissions(DB_PATH, company_id=company_id)
    except Exception:
        return None
    for s in subs:
        p = Path(s.get("stored_path") or "")
        if p.exists():
            try:
                recs, _r, _d = load_census(p, column_map=COLMAP)
                if recs:
                    return recs
            except Exception:
                continue
    return None


def _default_vdate(company_id: int) -> str:
    fs = store.get_funding_status(DB_PATH, company_id)
    if fs and fs.get("valuation_date"):
        return fs["valuation_date"]
    return f"{dt.date.today().year - 1}-12-31"


def _client_operation_status(user, company_id=None, kp=""):
    """사외적립자산 현황 — 양식 다운로드 → 작성 후 업로드 → 결과값 편집 미리보기(저장)."""
    cid = company_id or user["company_id"]
    st.markdown("### 💰 사외적립자산 현황")
    st.caption("화면에 하나하나 입력하지 않고, **양식을 내려받아 작성 → 업로드**하면 "
               "결과값을 아래 표에서 확인·수정하고 저장합니다. (DC형 자산 제외 · 회계장부와 일치)")
    st.download_button("📄 사외적립자산 양식 다운로드", AF.build_funding_template(_default_vdate(cid)),
                       file_name="사외적립자산_양식.xlsx", key=f"fund_tmpl_{kp}{cid}", width="stretch")

    up = st.file_uploader("작성한 사외적립자산 양식 업로드 (xlsx)", type=["xlsx"], key=f"fund_up_{kp}{cid}")
    edit_key = f"fund_edit_{kp}{cid}"
    if up is not None:
        try:
            parsed = AF.parse_funding_upload(bytes(up.getbuffer()))
        except Exception as e:  # noqa: BLE001
            st.error(f"양식을 읽을 수 없습니다: {e}")
            parsed = None
        if parsed is not None:
            st.session_state[edit_key] = parsed
            st.success("✅ 업로드 완료 — 아래 표에서 값을 확인·수정한 뒤 저장하세요.")

    # 편집 대상: 방금 업로드분 우선, 없으면 기존 저장값
    src = st.session_state.get(edit_key)
    if src is None:
        fs = store.get_funding_status(DB_PATH, cid)
        try:
            src = json.loads(fs["data_json"]) if fs and fs.get("data_json") else None
        except Exception:
            src = None
    if src is None:
        st.info("아직 등록된 사외적립자산 자료가 없습니다. 양식을 내려받아 작성·업로드하세요.")
        return

    vdate = src.get("_valuation_date") or _default_vdate(cid)
    fvd = st.text_input("작성기준일(산출기준일)", value=vdate, key=f"fund_vd_{kp}{cid}")
    rows = [{"항목": lb, "금액(원)": float(src.get(key) or 0.0)}
            for key, lb, _n in AF._FUNDING_ROWS]
    edited = st.data_editor(pd.DataFrame(rows), width="stretch", hide_index=True,
                            disabled=["항목"], key=f"fund_de_{kp}{cid}")
    disc_method = st.selectbox("국민연금전환금 공시방법", DISCLOSURE_METHODS,
                               index=_idx(DISCLOSURE_METHODS, src.get("공시방법")),
                               key=f"fund_disc_{kp}{cid}")
    # 기말잔액 검증(산식 A+B−C+D+E+F vs 입력)
    vals = {AF._FUNDING_ROWS[i][0]: float(edited.iloc[i]["금액(원)"] or 0)
            for i in range(len(AF._FUNDING_ROWS))}
    C = vals["지급_퇴직"] + vals["지급_중간정산"] + vals["지급_DC전환"]
    D = vals["관계사전입"] - vals["관계사전출"]
    E = vals["사업결합"] - vals["사업처분"]
    F = vals["투자수익"] - vals["운용수수료"]
    end_input = vals["기말_퇴직연금"] + vals["기말_퇴직신탁"] + vals["기말_퇴직보험"]
    formula = vals["기초잔액"] + vals["입금액"] - C + D + E + F
    m1, m2, m3 = st.columns(3)
    m1.metric("기말잔액(산식)", f"{formula:,.0f}", help="A + B − C + D + E + F")
    m2.metric("기말잔액(입력)", f"{end_input:,.0f}")
    diff = round(formula - end_input)
    m3.metric("차이", f"{diff:,.0f}")
    if diff != 0:
        st.warning("⚠️ 산식과 입력 기말잔액이 다릅니다 — 값을 재확인하세요.")
    else:
        st.success("✅ 산식 = 입력 (일치)")

    if st.button("💾 사외적립자산 현황 저장", type="primary", key=f"fund_save_{kp}{cid}"):
        save = dict(vals)
        save["공시방법"] = disc_method
        store.save_funding_status(DB_PATH, cid, fvd,
                                  json.dumps(save, ensure_ascii=False), user["id"], now())
        st.session_state.pop(edit_key, None)
        st.success("사외적립자산 현황을 저장했습니다.")


def _pii_check(raw: bytes, name: str):
    """(read_err, sensitive_list) — 실명·개인정보 사전 차단용."""
    probe, read_err = None, None
    try:
        probe = (pd.read_csv(io.BytesIO(raw), nrows=0) if name.lower().endswith(".csv")
                 else pd.read_excel(io.BytesIO(raw), nrows=0))
    except Exception as e:
        read_err = str(e)
    sensitive = detect_sensitive_columns(list(probe.columns)) if probe is not None else []
    return read_err, sensitive


def _show_pii_error(read_err, sensitive) -> bool:
    if read_err:
        st.error(f"파일을 읽을 수 없습니다: {read_err}")
        return True
    if sensitive:
        st.error("🚫 실명·개인정보로 보이는 항목이 있어 **업로드할 수 없습니다.** "
                 "해당 컬럼을 삭제/수정한 뒤 다시 올려주세요. (식별은 사번으로만)")
        for col, lbl, msg in sensitive:
            st.markdown(f"- **{col}** — {msg}")
        return True
    return False


def _save_census_file(raw: bytes, name: str, dest: Path):
    """민감 개인정보 컬럼을 자동 삭제하고 dest에 저장. (dropped_cols, read_err) 반환.

    - 실명·주민번호·주소·연락처 등으로 판단되는 컬럼은 제거 후 저장(자동삭제).
    - 민감 컬럼이 없으면 원본 그대로 저장(형식 보존).
    """
    try:
        if name.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw), dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    sens = {c for c, _lbl, _msg in detect_sensitive_columns(list(df.columns))}
    dropped = [c for c in df.columns if c in sens]
    if dropped:
        df = df.drop(columns=dropped)
        if name.lower().endswith(".csv"):
            df.to_csv(dest, index=False)
        else:
            df.to_excel(dest, index=False)
    else:
        dest.write_bytes(raw)
    return dropped, None


def _aux_list_ui(user, ctype):
    files = store.list_aux_census(DB_PATH, user["company_id"], ctype)
    if files:
        st.caption("업로드된 파일:")
        for f in files:
            c = st.columns([5, 1])
            p = Path(f["stored_path"])
            if p.exists():
                c[0].download_button(f"⬇ {f['filename']}", p.read_bytes(),
                                     file_name=f["filename"], key=f"auxdl_{f['id']}")
            else:
                c[0].write(f["filename"])
            if c[1].button("🗑", key=f"auxdel_{f['id']}"):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
                store.delete_aux_census(DB_PATH, f["id"])
                st.rerun()


def _retirement_ages(company_id: int):
    """회사가 입력한 정년(일반/계약직)·임원정년을 반환. (정년, 임원정년|None).

    임원정년이 0/미입력이면 None → 임원은 정년 검증에서 제외된다.
    """
    pi = store.get_plan_info(DB_PATH, company_id) or {}
    reg = int(pi.get("retirement_age") or 60)
    exec_age = None
    try:
        d = json.loads(pi["detail_json"]) if pi.get("detail_json") else {}
        ev = int(d.get("exec_retirement_age") or 0)
        exec_age = ev if ev > 0 else None
    except Exception:  # noqa: BLE001
        exec_age = None
    return reg, exec_age


def _plan_timing(company_id: int) -> str:
    """제도입력에 저장된 탈퇴·지급 시점. 미설정이면 연중(mid_year) 기본."""
    pi = store.get_plan_info(DB_PATH, company_id) or {}
    try:
        d = json.loads(pi["detail_json"]) if pi.get("detail_json") else {}
    except Exception:  # noqa: BLE001
        d = {}
    t = d.get("decrement_timing")
    return t if t in ("mid_year", "end_of_year") else DEFAULT_TIMING


def _aux_retiree_ids(company_id: int) -> list:
    """저장된 '퇴직자 및 DC전환명부'에서 사번 목록 추출(교차검증용)."""
    ids = []
    for f in store.list_aux_census(DB_PATH, company_id, "퇴직자 및 DC전환명부"):
        p = Path(f["stored_path"])
        if p.exists():
            ids += census_aux.read_ids(p)
    return ids


def _aux_transfer(company_id: int) -> list:
    """저장된 '전출입명부'에서 (사번, 사유) 목록 추출."""
    out = []
    for f in store.list_aux_census(DB_PATH, company_id, "전출입명부"):
        p = Path(f["stored_path"])
        if p.exists():
            out += census_aux.read_transfer(p)
    return out


def _load_prior_active(company_id: int) -> list:
    """저장된 '전기말재직자명부'(최신)에서 표준 레코드 로드 — 전년도 대비 검증용."""
    files = store.list_aux_census(DB_PATH, company_id, "전기말재직자명부")
    for f in files:
        p = Path(f["stored_path"])
        if p.exists():
            try:
                recs, _r, _d = load_census(p, column_map=COLMAP)
                return recs
            except Exception:  # noqa: BLE001
                return []
    return []


def _run_aux_cross(company_id: int, records, report):
    """보조명부(퇴직자·전출입)·전기말재직자명부가 있으면 교차검증을 report에 추가."""
    retiree = _aux_retiree_ids(company_id)
    transfer = _aux_transfer(company_id)
    prior = _load_prior_active(company_id)
    used = False
    if retiree or transfer:
        run_aux_cross_checks(records, retiree, transfer, report)
        used = True
    if prior:
        run_cross_year_checks(records, prior, retiree_ids=retiree, report=report)
        used = True
    return used


def _tables_from_base_set(set_id: int, size_band: str = "lt300"):
    """저장된 기초율 세트 → 엔진 DecrementTables (연령별 퇴직률·사망률 남/여).

    size_band: '300인 미만'('lt300') / '300인 이상'('ge300') — 개발원 밴드형 세트의
    퇴직률을 사업장 규모에 맞게 선택한다(밴드 컬럼이 없으면 단일 퇴직률 사용).
    """
    full = store.get_base_rate_set(DB_PATH, set_id)
    rows = json.loads(full["data_json"]) if full and full.get("data_json") else []
    ret = pd.DataFrame([{"age": r["age"], "rate": BR.band_withdrawal(r, size_band) or 0.0}
                        for r in rows])
    mort = pd.DataFrame([{"age": r["age"], "male_qx": r.get("mort_m") or 0.0,
                          "female_qx": r.get("mort_f") or 0.0} for r in rows])
    return DecrementTables(retirement_by_age=ret, retirement_by_service=None, mortality=mort)


def _rates_display_df(rows):
    """기초율 표시용 DataFrame — 밴드형이면 연령·사망률·퇴직률(미만/이상)·승급률(미적용/미만/이상)
    순서로만 보이고, 하위호환용 단일 퇴직율/승급률 열은 숨긴다."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "withdrawal_ge300" in df.columns or "withdrawal_lt300" in df.columns:
        order = ["age", "mort_m", "mort_f", "withdrawal_lt300", "withdrawal_ge300",
                 "raise_none", "raise_lt300", "raise_ge300"]
    else:
        order = ["age", "withdrawal", "mort_m", "mort_f", "raise_rate"]
    cols = [c for c in order if c in df.columns]
    return df[cols].rename(columns=BR.RATE_LABELS)


def _is_dev_set(rows) -> bool:
    """세트 데이터가 300인 밴드형(개발원)인지 판별."""
    return bool(rows) and any(r.get("withdrawal_ge300") is not None
                              or r.get("withdrawal_lt300") is not None for r in rows)


def _base_set_snapshot(set_id: int, size_band: str) -> dict:
    """산출에 사용한 기초율 세트를 스냅샷으로 보존(세트가 나중에 변경/삭제돼도 결과에 유지)."""
    full = store.get_base_rate_set(DB_PATH, set_id)
    if not full:
        return {}
    try:
        rows = json.loads(full["data_json"]) if full.get("data_json") else []
    except Exception:  # noqa: BLE001
        rows = []
    _kind = full.get("kind") or "dev"
    _is_exp = _kind == "experience"
    return {
        "id": full["id"], "name": full["name"], "source": full.get("source"),
        "base_year": full.get("base_year"), "period_kind": full.get("period_kind"),
        "retirement_age": full.get("retirement_age"), "avg_raise": full.get("avg_raise"),
        "kind": _kind,
        "size_band": size_band,
        # 경험 기초율은 규모 밴드 개념이 없음 → 라벨 생략
        "size_band_label": "" if _is_exp else ("300인 이상" if size_band == "ge300" else "300인 미만"),
        "rows": rows,
    }


def _client_census_form(user, val_date, purpose, edit_sid=None):
    st.warning("⚠️ 실명·주민번호·주소 등 개인정보 금지 — 명부 등록 시 불필요한 개인정보는 "
               "**자동 삭제**되어 등록되며, 모든 자료는 **DB 암호화**되어 저장됩니다.")
    ctype = st.radio("명부 종류", list(CT.CENSUS_TYPES.keys()), horizontal=True, key="census_type_sel")
    meta = CT.CENSUS_TYPES[ctype]

    with st.expander(f"📖 {ctype} 작성요령", expanded=False):
        for line in meta["guide"]:
            st.markdown(line if line[:1] in ("★", "【", "·", "ⓐ", "ⓑ", "ⓒ", "ⓓ") else f"- {line}")
    dl, _ = st.columns([1, 2])
    dl.download_button("📄 양식 다운로드", CT.build_template(meta["cols"], meta["guide"], ctype),
                       file_name=f"{ctype}_양식.xlsx", key=f"tmpl_{meta['code']}", width="stretch")

    up = st.file_uploader(f"{ctype} 파일 업로드 (xlsx / csv)", type=["xlsx", "csv"],
                          key=f"up_{meta['code']}")
    if up is None:
        _aux_list_ui(user, ctype) if ctype != "재직자명부" else None
        return
    raw = bytes(up.getbuffer())
    # 업로드 서명(파일명·크기·기준일) — 리런 때 같은 파일이면 재저장·중복생성하지 않음(이중접수 방지)
    sig = f"{meta['code']}|{up.name}|{len(raw)}|{val_date.isoformat()}"
    done_key = f"census_done_{meta['code']}_{user['company_id']}"
    prev = st.session_state.get(done_key)
    new_upload = not (isinstance(prev, dict) and prev.get("sig") == sig)

    if new_upload:
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        saved = FILES_DIR / f"c{user['company_id']}_{meta['code']}_{stamp}_{up.name}"
        dropped, read_err = _save_census_file(raw, up.name, saved)
        if read_err:
            st.error(f"파일을 읽을 수 없습니다: {read_err}")
            return
        st.session_state[done_key] = {"sig": sig, "saved": str(saved), "dropped": dropped}
        prev = st.session_state[done_key]
    else:
        saved = Path(prev["saved"])
        dropped = prev.get("dropped")
    if dropped:
        st.info(f"🔒 개인정보로 판단된 컬럼을 자동 삭제하고 등록했습니다: {', '.join(dropped)}")

    if ctype != "재직자명부":
        # 보조 명부: PII만 검사하고 저장(계리사가 참고). 정식 검증·계산 대상은 재직자명부.
        if new_upload:
            store.add_aux_census(DB_PATH, user["company_id"], ctype, up.name, str(saved),
                                 user["id"], now())
            st.success(f"✅ {ctype}를 저장했습니다. 계리사가 명부조회에서 확인합니다.")
        _aux_list_ui(user, ctype)
        return

    # 재직자명부: 정식 검증 → 제출 대상 생성/수정
    records, report, df = load_census(saved, column_map=COLMAP)
    validate_census(records, val_date, report)
    run_smart_checks(records, val_date, report)
    _reg_age, _exec_age = _retirement_ages(user["company_id"])
    run_actuary_checks(records, val_date, report,
                       retirement_age=_reg_age, exec_retirement_age=_exec_age)
    _run_aux_cross(user["company_id"], records, report)   # 퇴직자·전출입 교차검증

    m1, m2, m3 = st.columns(3)
    m1.metric("레코드", f"{report.n_records}건")
    m2.metric("오류", f"{len(report.errors)}건")
    m3.metric("경고", f"{len(report.warnings)}건")

    status = "needs_fix" if report.has_errors else "validated"

    def _create_now():
        sid = store.create_submission(
            DB_PATH, user["company_id"], user["id"], up.name, str(saved),
            val_date.isoformat(), status, report.n_records,
            len(report.errors), len(report.warnings), now(),
            purpose=purpose, applicant=user.get("display_name"))
        store.log_action(DB_PATH, user["id"], "upload", now(), f"submission#{sid} {status}")
        return sid

    if new_upload and edit_sid:
        # 수정모드: 해당 신청건의 명부를 제자리 갱신(새 건 생성/중복확인 없음)
        store.update_submission_file(DB_PATH, edit_sid, up.name, str(saved), report.n_records,
                                     len(report.errors), len(report.warnings), now())
        store.update_submission_status(DB_PATH, edit_sid, status, now())
        store.log_action(DB_PATH, user["id"], "reupload", now(), f"submission#{edit_sid} {status}")
        prev["sid"], prev["dupe"] = edit_sid, None
        st.success("명부를 교체했습니다(이 건 갱신).")
    elif new_upload:
        # 산출기준일은 기업당 하나만 — 같은 기준일 건이 있으면 접수여부에 따라 처리
        same = [s for s in store.list_submissions(DB_PATH, company_id=user["company_id"])
                if s["valuation_date"] == val_date.isoformat()]
        blocked = [s for s in same if s["status"] not in store.CLIENT_EDITABLE]
        editable = [s for s in same if s["status"] in store.CLIENT_EDITABLE]
        if blocked:
            prev["dupe"], prev["sid"] = "blocked", None
        elif editable:
            prev["dupe"] = "replace"
            prev["replace_ids"] = [s["id"] for s in editable]
            prev["sid"] = None
        else:
            prev["sid"], prev["dupe"] = _create_now(), None

    # 중복 산출기준일 처리
    if prev.get("dupe") == "blocked":
        st.error(f"⛔ 산출기준일 **{val_date}** 건이 이미 **접수완료(또는 그 이후)** 상태입니다. "
                 "새로 올릴 수 없습니다. 수정이 필요하면 계리사에게 문의하세요.")
    elif prev.get("dupe") == "replace":
        st.warning(f"⚠️ 같은 산출기준일(**{val_date}**) 신청건이 이미 있습니다. "
                   "산출기준일은 기업당 하나만 등록됩니다.")
        rc1, rc2 = st.columns([2, 4])
        if rc1.button("🗑 기존 삭제하고 새로 등록", type="primary", key=f"repl_{meta['code']}"):
            for oid in prev.get("replace_ids", []):
                old = store.get_submission(DB_PATH, oid)
                if old:
                    try:
                        Path(old["stored_path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                    store.delete_submission(DB_PATH, oid)
            prev["sid"], prev["dupe"] = _create_now(), None
            st.success("기존 건을 삭제하고 새로 등록했습니다.")
            st.rerun()
        rc2.caption("기존 신청건을 삭제하고 이 명부로 새로 등록합니다. (접수 전 건만 가능)")

    if prev.get("sid"):
        if report.has_errors:
            st.warning("⚠️ 오류가 있습니다. 아래에서 **오류만 조회·다운로드**해 수정 후 다시 올리거나, "
                       "**신청하기**로 이대로 신청할 수 있습니다.")
        elif len(report.warnings):
            st.info("경고가 있지만 신청은 가능합니다. 아래에서 확인하세요.")
        else:
            st.success("✅ 오류 없음! 아래 **신청하기**를 누르세요.")
    try:
        raw_df = pd.read_excel(saved) if str(saved).lower().endswith((".xlsx", ".xls")) \
            else pd.read_csv(saved)
    except Exception:  # noqa: BLE001
        raw_df = None
    _error_review_ui(df, report, f"up_{user['company_id']}", dropped=dropped,
                     summary={"filename": up.name, "valuation_date": val_date.isoformat()},
                     raw_df=raw_df)

    # 오류를 고치지 않고 사유와 함께 제출하려는 경우 — 계리사에게 전달할 메모
    if prev.get("sid") and (report.has_errors or report.warnings):
        with st.expander("❗ 오류를 수정하지 않고 사유와 함께 신청하기", expanded=False):
            note = st.text_area("오류/경고가 실제 오류가 아니라면 사유를 적어주세요 (계리사에게 전달)",
                                key=f"errnote_{prev['sid']}",
                                placeholder="예: ih210003은 촉탁 재입사자로 정년초과 정상입니다.")
            if st.button("💬 사유 저장(신청 건에 첨부)", key=f"errnotesave_{prev['sid']}"):
                store.set_submission_meta(DB_PATH, prev["sid"], note=note.strip())
                st.success("사유를 신청 건에 첨부했습니다. 신청하기를 누르면 함께 전달됩니다.")


def _client_experience_data(user):
    """경험기초율 산출데이터 업로드 — 회사의 실제 퇴직·급여상승 경험 자료(계리사가 경험률 산출)."""
    st.caption("회사의 실제 경험(퇴직·급여상승)으로 **경험기초율(경험 퇴직률·승급률)** 을 산출하기 위한 "
               "자료입니다. 표준양식을 내려받아 관측기간과 종업원 이력을 채워 올리면, 계리사가 경험률을 "
               "산출해 산출에 반영합니다.")
    st.warning("⚠️ 실명·주민번호 등 개인정보 금지 — 불필요한 개인정보는 **자동 삭제**되어 등록됩니다.")
    with st.expander("📖 작성요령", expanded=False):
        for line in [
            "· '데이터' 시트 상단 관측시작일·관측종료일을 먼저 채우세요(최근 3~5년, yyyymmdd).",
            "· 관측기간 재직자(기간 중 퇴직자 포함) 1인 1행. 사원번호만 사용(실명 금지).",
            "· 관측시작급여/관측종료급여: 시작·종료 시점 월 기준급여. 상태: 재직 1 / 퇴직 2(퇴직일 필수).",
            "· 표본이 많을수록(특히 300인 이상·3년 이상) 경험률 신뢰도가 높습니다.",
            "· 사망률은 개발원 기초율에서 차용합니다.",
        ]:
            st.markdown(f"- {line}")
    st.download_button("📄 경험기초율 산출데이터 양식 다운로드", EXP.build_experience_template(),
                       file_name="경험기초율_산출데이터_양식.xlsx", key="exp_tmpl", width="stretch")

    up = st.file_uploader("경험기초율 산출데이터 업로드 (xlsx / csv)", type=["xlsx", "csv"], key="exp_up")
    if up is not None:
        raw = bytes(up.getbuffer())
        sig = f"exp|{up.name}|{len(raw)}"
        done_key = f"exp_done_{user['company_id']}"
        prev = st.session_state.get(done_key)
        if not (isinstance(prev, dict) and prev.get("sig") == sig):
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            saved = FILES_DIR / f"c{user['company_id']}_experience_{stamp}_{up.name}"
            dropped, read_err = _save_census_file(raw, up.name, saved)
            if read_err:
                st.error(f"파일을 읽을 수 없습니다: {read_err}")
                return
            store.add_aux_census(DB_PATH, user["company_id"], store.EXPERIENCE_CENSUS_TYPE,
                                 up.name, str(saved), user["id"], now())
            st.session_state[done_key] = {"sig": sig}
            if dropped:
                st.info(f"🔒 개인정보로 판단된 컬럼을 자동 삭제했습니다: {', '.join(dropped)}")
            st.success("✅ 경험기초율 산출데이터를 등록했습니다. 계리사가 경험률을 산출합니다.")
    _aux_list_ui(user, store.EXPERIENCE_CENSUS_TYPE)


def _client_other_lt(user, company_id=None, kp=""):
    """기타장기 포상제도 — 양식(사용자 원본) 다운로드 → 작성 후 업로드(파일 저장·미리보기)."""
    cid = company_id or user["company_id"]
    st.caption("장기근속 포상 등 **기타장기종업원급여** 자료입니다. 화면에 하나하나 입력하지 않고 "
               "**양식을 내려받아 작성 → 업로드**하면 파일이 저장되고 아래에 내용이 표시됩니다.")
    st.download_button("📄 기타장기 포상제도 양식 다운로드", AF.build_other_lt_template(),
                       file_name="기타장기_포상제도_양식.xlsx", key=f"olt_tmpl_{kp}{cid}", width="stretch")

    up = st.file_uploader("작성한 기타장기 양식 업로드 (xlsx)", type=["xlsx"], key=f"olt_up_{kp}{cid}")
    if up is not None:
        raw = bytes(up.getbuffer())
        sig = f"olt|{up.name}|{len(raw)}"
        done_key = f"olt_done_{kp}{cid}"
        if st.session_state.get(done_key) != sig:
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            saved = FILES_DIR / f"c{cid}_oltform_{stamp}_{up.name}"
            saved.write_bytes(raw)
            store.add_aux_census(DB_PATH, cid, "기타장기포상제도", up.name, str(saved),
                                 user["id"], now())
            st.session_state[done_key] = sig
            st.success("✅ 기타장기 포상제도 자료를 저장했습니다.")

    files = store.list_aux_census(DB_PATH, cid, "기타장기포상제도")
    if files:
        latest = files[0]
        p = Path(latest["stored_path"])
        if p.exists():
            st.download_button(f"⬇ 저장된 파일: {latest['filename']}", p.read_bytes(),
                               file_name=latest["filename"], key=f"oltdl_{kp}{cid}")
            try:
                prev = pd.read_excel(p, header=None).fillna("")
                st.caption("업로드된 내용 미리보기")
                st.dataframe(prev, width="stretch", hide_index=True)
            except Exception:
                pass

    st.divider()
    st.markdown("**기타장기 재직자 명부** (대상자 명부 — 별도 등록)")
    meta = CT.OTHER_LT_CENSUS
    st.download_button("📄 기타장기 재직자명부 양식 다운로드",
                       CT.build_template(meta["cols"], meta["guide"], "기타장기 재직자명부"),
                       file_name="기타장기_재직자명부_양식.xlsx", key=f"tmpl_olt_{kp}{cid}")
    up2 = st.file_uploader("기타장기 재직자 명부 업로드", type=["xlsx", "csv"], key=f"up_olt_{kp}{cid}")
    if up2 is not None:
        raw = bytes(up2.getbuffer())
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        saved = FILES_DIR / f"c{cid}_otlt_{stamp}_{up2.name}"
        dropped, read_err = _save_census_file(raw, up2.name, saved)
        if read_err:
            st.error(f"파일을 읽을 수 없습니다: {read_err}")
        else:
            if dropped:
                st.info(f"🔒 개인정보 컬럼을 자동 삭제하고 등록했습니다: {', '.join(dropped)}")
            store.add_aux_census(DB_PATH, cid, "기타장기재직자명부",
                                 up2.name, str(saved), user["id"], now())
            st.success("✅ 기타장기 재직자 명부를 저장했습니다.")
    _aux_list_ui(user, "기타장기재직자명부") if not company_id else _aux_list_ui(
        {"company_id": cid}, "기타장기재직자명부")


def _client_census_summary(user, company_id=None, kp=""):
    """명부확인용 요약표 — 재직자명부 등록 후에만. 양식 다운로드 시 G열 자동산출."""
    cid = company_id or user["company_id"]
    st.caption("회사 요약장표와 명부 기재내용이 다른 경우가 많아 **추가 확인용**입니다. "
               "양식을 내려받으면 **‘등록부명에서 자동산출된 값’(G열)** 이 재직자명부에서 자동 계산되어 채워집니다. "
               "F열(녹색)만 회사가 확인·입력해 업로드하세요.")
    recs = _latest_active_census_records(cid)
    if not recs:
        st.warning("⚠️ 먼저 **재직자명부**를 등록해 주세요. 명부확인용 요약표는 재직자명부 등록 후에만 작성할 수 있습니다.")
        return
    summary = AF.compute_census_summary(recs)
    cA, cB, cC, cD = st.columns(4)
    cA.metric("재직자 합계", f"{summary['재직_합계']:,}명")
    cB.metric("임원/직원/계약직", f"{summary['재직_임원']}/{summary['재직_직원']}/{summary['재직_계약직']}")
    cC.metric("추계액 합계", f"{summary['추계액_합계']:,.0f}")
    cD.metric("중간정산자", f"{summary['중간정산자수']}명")
    st.download_button("📄 명부확인용 요약표 양식 다운로드 (G열 자동산출 포함)",
                       AF.build_census_summary_template(summary, _default_vdate(cid)),
                       file_name="명부확인용_요약표_양식.xlsx", key=f"sum_tmpl_{kp}{cid}", width="stretch")

    up = st.file_uploader("작성한 명부확인용 요약표 업로드 (xlsx)", type=["xlsx"], key=f"sum_up_{kp}{cid}")
    edit_key = f"sum_edit_{kp}{cid}"
    if up is not None:
        try:
            parsed = AF.parse_census_summary_upload(bytes(up.getbuffer()))
            st.session_state[edit_key] = parsed
            store.save_census_summary(DB_PATH, cid, json.dumps(parsed, ensure_ascii=False),
                                      user["id"], now())
            st.success("✅ 업로드 완료 — 아래 표에서 확인하세요. (자동산출값과 회사 입력값 비교)")
        except Exception as e:  # noqa: BLE001
            st.error(f"양식을 읽을 수 없습니다: {e}")

    src = st.session_state.get(edit_key)
    if src is None:
        rec = store.get_census_summary(DB_PATH, cid)
        try:
            src = json.loads(rec["data_json"]) if rec and rec.get("data_json") else None
        except Exception:
            src = None
    if src and src.get("rows"):
        df = pd.DataFrame(src["rows"])
        st.markdown("**업로드된 요약표 (자동산출값 vs 회사 입력값)**")
        st.dataframe(df, width="stretch", hide_index=True)


def _client_prior_form(user):
    st.caption("이전 계리법인에서 받은 자료를 입력하거나 스캔·사진·PDF로 올려주세요.")
    pr = store.get_prior_record(DB_PATH, user["company_id"]) or {}
    with st.form("prior_rec"):
        a, b = st.columns(2)
        pf = a.text_input("전 계리법인", pr.get("prior_firm") or "")
        pvd = b.text_input("전기 산출기준일", pr.get("prior_valuation_date") or "", placeholder="예: 2024-12-31")
        pdbo = a.number_input("전기말 확정급여채무(원)", value=float(pr.get("prior_dbo") or 0.0), step=1_000_000.0)
        pdisc = b.number_input("전기 할인율(%)", value=float(pr.get("prior_discount_rate") or 0.0), step=0.1)
        psal = a.number_input("전기 임금상승률(%)", value=float(pr.get("prior_salary_rate") or 0.0), step=0.1)
        pnotes = st.text_area("비고", pr.get("prior_notes") or "")
        if st.form_submit_button("💾 과거자료 저장"):
            store.save_prior_record(DB_PATH, user["company_id"], {
                "prior_firm": pf, "prior_valuation_date": pvd, "prior_dbo": float(pdbo),
                "prior_discount_rate": float(pdisc), "prior_salary_rate": float(psal),
                "prior_notes": pnotes,
            }, user["id"], now())
            st.success("과거자료를 저장했습니다.")

    pfiles = st.file_uploader("스캔·사진·PDF 업로드 (여러 개 가능)",
                              type=["pdf", "jpg", "jpeg", "png"], accept_multiple_files=True,
                              key="prior_files")
    if pfiles and st.button("📎 업로드한 파일 저장"):
        d = FILES_DIR / "prior" / f"c{user['company_id']}"
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        for f in pfiles:
            p = d / f"{stamp}_{f.name}"
            p.write_bytes(f.getbuffer())
            store.add_prior_file(DB_PATH, user["company_id"], f.name, str(p), user["id"], now())
        st.success(f"{len(pfiles)}개 파일을 저장했습니다.")
    saved_files = store.list_prior_files(DB_PATH, user["company_id"])
    if saved_files:
        st.caption("업로드된 자료:")
        for f in saved_files:
            st.write(f"- 📄 {f['filename']}  ({f['created'][:16].replace('T', ' ')})")


# ── 기업 기본 정보 ──────────────────────────────────────────────────────────
def _client_company_info(user):
    sv = store.get_sales(DB_PATH, user["company_id"]) or {}
    with st.form("company_info"):
        a, b = st.columns(2)
        cname = a.text_input("담 당 자", sv.get("contact_name") or "")
        cmonth = b.text_input("결 산 월", sv.get("settlement_month") or "", placeholder="예: 12월")
        office = a.text_input("전화번호 (사무실)", sv.get("contact_phone") or "")
        mobile = b.text_input("전화번호 (모바일)", sv.get("contact_mobile") or "")
        email = a.text_input("이 메 일", sv.get("contact_email") or "")
        addr = st.text_input("주        소", sv.get("address") or "")
        if st.form_submit_button("💾 입력 / 수정 저장", type="primary"):
            merged = {k: sv.get(k) for k in store.SALES_FIELDS}
            merged.update({
                "contact_name": cname, "contact_phone": office, "contact_mobile": mobile,
                "contact_email": email, "settlement_month": cmonth, "address": addr,
            })
            merged.setdefault("received_date", dt.date.today().isoformat())
            store.save_sales(DB_PATH, user["company_id"], merged, user["id"], now())
            st.success("기업 기본 정보를 저장했습니다.")


# ---------------------------------------------------------------------------
# 계리인 (actuary)
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip()) or "회사"


def _archive_completed_report(sub, res) -> Path:
    """확정된 보고서를 산출일(valuation_date) 기준 폴더로 복사·정리한다.

    구조: data/platform/완료보고서/<산출일>/<회사명_sub{id}>/<회사명_파일명>
    """
    vdate = sub["valuation_date"]                      # YYYY-MM-DD
    company = _safe_name(sub["company_name"])
    dest = ARCHIVE_DIR / vdate / f"{company}_sub{sub['id']}"
    dest.mkdir(parents=True, exist_ok=True)
    src_dir = Path(res["xlsx_path"]).parent
    for name in ("dbo_report.pptx", "dbo_report.xlsx", "dbo_results.xlsx", "run_log.json"):
        f = src_dir / name
        if f.exists():
            shutil.copy2(f, dest / f"{company}_{name}")
    return dest


def _report_email_ui(user, sub, rep_pptx: Path, rep_xlsx: Path):
    """보고서를 담당자에게 이메일로 보내는 UI (초안 .eml 만들기 + SMTP 즉시전송)."""
    with st.expander("📧 보고서 이메일로 보내기", expanded=False):
        sales = store.get_sales(DB_PATH, sub["company_id"]) or {}
        default_to = sales.get("contact_email") or ""
        y = sub["valuation_date"][:4]
        to = st.text_input("받는 사람 이메일 (보고서 담당자)", value=default_to,
                           key=f"mail_to_{sub['id']}", placeholder="예: manager@company.com")
        subject = st.text_input("제목",
                                value=f"[{sub['company_name']}] {y}년 K-IFRS 제1019호 계리평가보고서",
                                key=f"mail_subj_{sub['id']}")
        body = st.text_area(
            "메시지",
            value=(f"{sub['company_name']} 담당자님,\n\n"
                   f"{y}년 K-IFRS 제1019호 종업원급여 계리평가보고서를 첨부드립니다.\n"
                   f"파워포인트(요약)와 엑셀(주석공시 상세)을 함께 보내드립니다.\n\n"
                   f"문의사항은 회신 부탁드립니다.\n\n감사합니다."),
            key=f"mail_body_{sub['id']}", height=140)
        c1, c2 = st.columns(2)
        inc_ppt = c1.checkbox("📊 파워포인트 첨부", value=True, key=f"mail_ppt_{sub['id']}")
        inc_xlsx = c2.checkbox("📑 엑셀 전문서식 첨부", value=True, key=f"mail_xlsx_{sub['id']}")

        atts = []
        if inc_ppt and rep_pptx.exists():
            atts.append(rep_pptx)
        if inc_xlsx and rep_xlsx.exists():
            atts.append(rep_xlsx)

        if not mailer.valid_email(to):
            st.caption("받는 사람 이메일을 입력하면 전송/초안 버튼이 활성화됩니다.")
            return

        from_addr = user.get("email") or "no-reply@dbo-platform.local"
        e1, e2 = st.columns(2)

        # 1) 초안(.eml) 내려받기 — SMTP 없이도 항상 가능
        eml = mailer.build_eml(from_addr, [to], subject, body, attachments=atts,
                               from_name=user.get("display_name", ""))
        e1.download_button("📥 이메일 초안(.eml) 만들기", eml,
                           file_name=f"{sub['company_name']}_보고서메일.eml",
                           key=f"mail_eml_{sub['id']}", width="stretch",
                           help="내려받아 더블클릭하면 아웃룩 등에서 첨부까지 채워진 채로 열립니다.")

        # 2) 바로 보내기 — 환경변수로 SMTP가 설정된 경우
        cfg = mailer.smtp_config_from_env()
        if cfg:
            if e2.button("📧 지금 바로 보내기", key=f"mail_send_{sub['id']}",
                         type="primary", width="stretch"):
                ok, msgtxt = mailer.send_smtp(cfg, [to], subject, body, attachments=atts,
                                              from_name=user.get("display_name", ""))
                if ok:
                    store.log_action(DB_PATH, user["id"], "email_report", now(),
                                     f"submission#{sub['id']} → {to}")
                    st.success(f"✅ {msgtxt}")
                else:
                    st.error(msgtxt)
        else:
            e2.caption("‘바로 보내기’는 서버(SMTP) 설정 시 활성화됩니다. "
                       "지금은 왼쪽 초안(.eml)으로 보내세요.")


def _plan_readonly(company_id):
    """기업이 입력한 제도 정보를 계리사 화면에 동일 내용으로 표시(읽기전용)."""
    pi = store.get_plan_info(DB_PATH, company_id)
    if not pi:
        st.info("기업이 아직 제도 정보를 입력하지 않았습니다. 입력을 요청하세요.")
        return
    try:
        d = json.loads(pi["detail_json"]) if pi.get("detail_json") else {}
    except Exception:
        d = {}

    def tbl(title, rows):
        data = [{"항목": k, "내용": str(v)} for k, v in rows if v not in (None, "", 0.0)]
        if data:
            st.markdown(f"**{title}**")
            st.table(pd.DataFrame(data))

    tbl("① 제도 일반", [
        ("제도 유형", pi.get("plan_type")), ("제도 설정일", pi.get("established_date")),
        ("퇴직금 규정", pi.get("benefit_rule")), ("급여 산정 기준", pi.get("salary_basis")),
        ("누진제/기타 서술", d.get("benefit_rule_desc")),
    ])
    tbl("② 정년·근무기간", [
        ("정년 퇴직연령", f"만 {pi.get('retirement_age')}세" if pi.get("retirement_age") else None),
        ("임원 퇴직연령", f"만 {d.get('exec_retirement_age')}세" if d.get("exec_retirement_age") else None),
        ("임금피크제", f"적용 (만 {d.get('wage_peak_age')}세~)" if d.get("wage_peak_applied") else None),
        ("지급률 가감", d.get("rate_adjust_desc")),
        ("1년미만 처리", d.get("under1yr_method")),
        ("정년해당자 당기근무원가", "산출" if d.get("normal_cost_at_retirement") else "미산출"),
        ("중간정산 허용", "예" if pi.get("interim_allowed") else "아니오"),
        ("중간정산 주기", pi.get("interim_cycle")),
    ])
    _disc = d.get("discount_basis")
    if _disc == "기타" and d.get("discount_basis_other"):
        _disc = f"기타({d['discount_basis_other']})"
    tbl("③ 할인율 및 사외적립비율", [
        ("할인율 산출기준", _disc),
        ("사외적립자산 적립비율(%)", pi.get("funding_ratio")),
    ])
    tbl("④ 임금인상률", [
        ("임금체계", d.get("salary_system")),
        ("고객제안 인상율(%)", d.get("baseup_proposed")),
    ])
    if d.get("baseup") and any(d["baseup"].values()):
        st.caption("과거 5년 Base-up(%)")
        st.dataframe(pd.DataFrame([d["baseup"]]), width="stretch", hide_index=True)
    if d.get("baseup_by_group"):
        st.caption("직군별 임금인상률(%)")
        st.dataframe(pd.DataFrame(d["baseup_by_group"]), width="stretch", hide_index=True)
    if pi.get("notes"):
        st.caption(f"특이사항: {pi['notes']}")
    for f in store.list_aux_census(DB_PATH, company_id, "누진제규정서류"):
        p = Path(f["stored_path"])
        if p.exists():
            st.download_button(f"⬇ [규정서류] {f['filename']}", p.read_bytes(),
                               file_name=f["filename"], key=f"prule_{f['id']}")


def _funding_readonly(company_id):
    """기업이 입력한 사외적립자산 현황을 계리사 화면에 동일 내용으로 표시."""
    fs = store.get_funding_status(DB_PATH, company_id)
    if not fs or not fs.get("data_json"):
        st.info("기업이 아직 사외적립자산 현황을 입력하지 않았습니다.")
        return
    try:
        fd = json.loads(fs["data_json"])
    except Exception:
        fd = {}
    st.caption(f"작성기준일: {fs.get('valuation_date') or '-'}")
    st.dataframe(pd.DataFrame([{"항목": lb, "금액": fd.get(k, 0)} for k, lb in FUNDING_ITEMS]),
                 width="stretch", hide_index=True)
    g = lambda k: float(fd.get(k) or 0)  # noqa: E731
    C = g("지급_퇴직") + g("지급_중간정산") + g("지급_DC전환")
    D = g("관계사전입") - g("관계사전출")
    E = g("사업결합") - g("사업처분")
    F = g("투자수익") - g("운용수수료")
    formula = g("기초잔액") + g("입금액") - C + D + E + F
    end_input = g("기말_퇴직연금") + g("기말_퇴직신탁") + g("기말_퇴직보험")
    m1, m2, m3 = st.columns(3)
    m1.metric("기말잔액(산식)", f"{formula:,.0f}")
    m2.metric("기말잔액(입력)", f"{end_input:,.0f}")
    m3.metric("차이", f"{round(formula - end_input):,.0f}")
    st.caption(f"공시방법: {fd.get('공시방법', '-')}")


def _compute_result_metrics(records, result, config, tables, ret_age, company_id) -> dict:
    """산출결과 상세지표(듀레이션·평균근속·민감도·만기·재직자현황·전기PBO)를 계산."""
    from dbo.report import _reprice, _bucketize, _BANDS, _class_stats
    from dbo.engine import expected_cashflows
    cf = expected_cashflows(records, config, tables)
    tot_pv = float(cf["현재가치"].sum()) if len(cf) else 0.0
    duration = float((cf["연도"] * cf["현재가치"]).sum() / tot_pv) if tot_pv else 0.0
    rem = [max(0.0, float(ret_age) - r.attained_age) for r in result.results]
    avg_service = sum(rem) / len(rem) if rem else 0.0
    prior = store.get_prior_record(DB_PATH, company_id) or {}
    pv_b = _bucketize(cf, "현재가치") if len(cf) else {}
    pay_b = _bucketize(cf, "기대급여지급액") if len(cf) else {}
    maturity = [{"만기구간": b, "확정급여채무": round(pv_b.get(b, 0.0)),
                 "예상지급액": round(pay_b.get(b, 0.0))} for b in _BANDS]
    stats, total = _class_stats(result)
    class_rows = [{"구분": nm, "인원": n, "평균연령": round(a, 1), "평균근속": round(sv, 1),
                   "평균임금합": round(ss)} for nm, n, a, sv, ss in stats + [total]]
    return {
        "pbo": float(result.total_dbo), "csc": float(result.total_csc),
        "n_calc": len(result.results), "n_excluded": len(result.excluded_emp_ids),
        "duration": duration, "avg_service": avg_service,
        "salary_rate": config.salary_increase_rate.flat,
        "disc_rate": config.discount_rate.flat, "ret_age": int(ret_age),
        "pbo_disc_low": _reprice(records, config, tables, dd=-0.01),   # 할인율 -1%p → PBO↑
        "pbo_disc_high": _reprice(records, config, tables, dd=+0.01),  # 할인율 +1%p → PBO↓
        "pbo_infl_low": _reprice(records, config, tables, sd=-0.01),
        "pbo_infl_high": _reprice(records, config, tables, sd=+0.01),
        "prior_pbo": float(prior.get("prior_dbo") or 0.0),
        "simple_dbo": float(result.subtotal_by_plan.get(2, {}).get("DBO", 0.0)),
        "maturity": maturity, "class_stats": class_rows,
    }


def _render_result_blocks(res):
    """산출결과 종합: 요약 · 민감도 표 · 만기구성 · 재직자현황 · 전기대비."""
    m = json.loads(res["metrics_json"]) if res.get("metrics_json") else {}
    pbo = m.get("pbo", res["total_dbo"])
    if not m:
        st.warning("이 결과에는 상세지표가 없습니다(이전 버전 계산). "
                   "**[🟨 계산 실행 (시뮬레이션)]** 을 다시 눌러 민감도·만기 등을 채워주세요.")

    # ── 요약 ──
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("전체 PBO(확정급여채무)", eok(pbo))
    k2.metric("당기근무원가(CSC)", eok(res["total_csc"]))
    k3.metric("계산대상", f"{res['n_calc']}명", help=f"제외 {m.get('n_excluded', 0)}명")
    k4.metric("가중평균만기", f"{m.get('duration', 0):.2f}년")
    st.caption(f"평균기대근속 {m.get('avg_service', 0):.2f}년 · "
               f"적용 할인율 {m.get('disc_rate', 0) * 100:.2f}% · "
               f"임금상승률 {m.get('salary_rate', 0) * 100:.2f}%"
               + (f" · 간편법(제도2) {eok(m.get('simple_dbo'))}" if m.get("simple_dbo") else ""))

    # ── 민감도 분석 (±1%p, 변화액·변화율) ──
    st.markdown("##### 📉 민감도 분석 (±1.00%p)")
    base = pbo or 1

    def srow(name, up, dn):
        return {
            "가정": name,
            "+1%p": round(up), "기준": round(pbo), "-1%p": round(dn),
            "+1%p 변화액": round(up - pbo), "+1%p 변화율": f"{(up / base - 1) * 100:+.2f}%",
            "-1%p 변화액": round(dn - pbo), "-1%p 변화율": f"{(dn / base - 1) * 100:+.2f}%",
        }
    st.dataframe(pd.DataFrame([
        srow("할인율", m.get("pbo_disc_high", 0), m.get("pbo_disc_low", 0)),
        srow("임금상승률", m.get("pbo_infl_high", 0), m.get("pbo_infl_low", 0)),
    ]), width="stretch", hide_index=True)
    st.caption("할인율이 낮아지면(−1%p) 확정급여채무 증가, 임금상승률이 높아지면(+1%p) 증가합니다.")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("##### ⏳ 만기구성 (확정급여채무 현재가치 · 예상지급액)")
        if m.get("maturity"):
            st.dataframe(pd.DataFrame(m["maturity"]), width="stretch", hide_index=True, height=320)
        else:
            st.caption("재계산하면 표시됩니다.")
    with c2:
        st.markdown("##### 👥 재직자 현황 (구분별)")
        if m.get("class_stats"):
            st.dataframe(pd.DataFrame(m["class_stats"]), width="stretch", hide_index=True)
        st.markdown("##### 📆 전기 대비")
        prior = m.get("prior_pbo", 0)
        if prior:
            st.write(f"- 당기 PBO: **{eok(pbo)}**")
            st.write(f"- 전기 PBO(전기자료): {eok(prior)}")
            st.write(f"- 증감: {eok(pbo - prior)} ({(pbo / prior - 1) * 100:+.1f}%)")
        else:
            st.caption("전기 계리자료(지난보고서 등록)가 있으면 전기 대비 증감을 표시합니다.")

    # ── 사용 기초율(감사추적 · 스냅샷 보존) ──
    br = m.get("base_rates")
    if br:
        _band = br.get("size_band_label", "")
        st.markdown("##### 📊 사용 기초율 (이 산출에 적용된 값 · 보존됨)")
        st.caption(
            f"세트: **{br.get('name', '')}**"
            + (f" · 출처 {br.get('source')}" if br.get("source") else "")
            + (f" · 기준연도 {br.get('base_year')}" if br.get("base_year") else "")
            + (f" · 사업장규모 **{_band}**" if _band else "")
            + (f" · 정년 {br.get('retirement_age')}" if br.get("retirement_age") else "")
            + " — 기초율 세트가 이후 변경·삭제되어도 이 결과에 적용된 값은 그대로 보존됩니다.")
        _brows = br.get("rows") or []
        if _brows:
            st.dataframe(_rates_display_df(_brows),
                         width="stretch", hide_index=True, height=260)


def page_actuary(user):
    st.markdown("## 🧮 계리사 전용")
    pending = store.qa_pending_counts(DB_PATH)
    total_pending = sum(pending.values())
    qa_label = f"💬 질의응답 ({total_pending})" if total_pending else "💬 질의응답"
    _exp_pending = sum(1 for x in store.experience_upload_status(DB_PATH) if x["n_sets"] == 0)
    br_label = f"📊 기초율 관리 🔴{_exp_pending}" if _exp_pending else "📊 기초율 관리"
    tab_list, tab_qa, tab_br, tab_dc = st.tabs(
        ["🏢 기업조회", qa_label, br_label, "📉 할인율 관리"])
    with tab_list:
        _actuary_company_lookup(user, pending)
    with tab_qa:
        _actuary_qa(user, pending)
    with tab_br:
        _actuary_base_rates(user)
    with tab_dc:
        _actuary_discount(user)


def _actuary_company_lookup(user, pending=None):
    pending = pending or {}
    st.subheader("1. 기업조회")
    st.caption("맨 앞 **선택** 버튼으로 작업할 기업을 고르면 아래에 바로 열립니다. **삭제**는 확인 후 삭제됩니다.")
    # 계리사는 신청(접수)된 건을 본다. 취소 건은 목록에 두지 않는다(삭제로 처리).
    subs = [s for s in store.list_submissions(DB_PATH)
            if s["status"] in ("submitted", "accepted", "calculated", "on_hold",
                                "client_review", "reported")]
    if not subs:
        st.info("아직 접수(신청)된 기업이 없습니다.")
        return

    f1, f2, f3 = st.columns(3)
    q_name = f1.text_input("🔍 기업명", key="al_name", placeholder="예: 라진")
    vdates = ["전체"] + sorted({s["valuation_date"] for s in subs}, reverse=True)
    q_vdate = f2.selectbox("산출기준일", vdates, key="al_vdate")
    calcs = ["전체"] + sorted({(s["calculator"] or "-") for s in subs})
    q_calc = f3.selectbox("산출자", calcs, key="al_calc")

    qn = (q_name or "").strip().lower()
    rows = [s for s in subs
            if (not qn or qn in (s["company_name"] or "").lower())
            and (q_vdate == "전체" or s["valuation_date"] == q_vdate)
            and (q_calc == "전체" or (s["calculator"] or "-") == q_calc)]
    if not rows:
        st.info("조건에 맞는 기업이 없습니다.")
        return

    _render_submission_list(user, rows, "actuary", pending, show_edit=False)
    # 계리사는 선택된 건(없으면 첫 건)의 작업화면을 항상 표시 — 제도조회·명부·부채계산 등
    sel = st.session_state.get("actuary_sel_sid")
    if sel not in [s["id"] for s in rows]:
        sel = rows[0]["id"]
    st.divider()
    st.caption("↑ 목록에서 **선택(⚪)** 하면 아래 작업화면이 그 기업으로 바뀝니다.")
    _actuary_work_detail(user, sel)


def _actuary_qa(user, pending=None):
    pending = pending if pending is not None else store.qa_pending_counts(DB_PATH)
    companies = store.list_companies(DB_PATH)
    if not companies:
        st.info("등록된 기업이 없습니다.")
        return
    names = {c["id"]: c["name"] for c in companies}

    total = sum(pending.values())
    if total:
        waiting = ", ".join(f"{names.get(cid, cid)} 💬{n}" for cid, n in pending.items() if n)
        st.warning(f"답변 대기 {total}건 — {waiting}")
    else:
        st.success("답변 대기 중인 질의가 없습니다.")

    def _fmt(i):
        n = pending.get(i, 0)
        return f"{names[i]}  💬{n}" if n else names[i]

    # 답변 대기 회사를 앞으로 정렬
    order = sorted(names, key=lambda i: (-(pending.get(i, 0)), names[i]))
    cid = st.selectbox("기업 선택", order, format_func=_fmt, key="aqa_company")
    _render_qa_panel(cid, "actuary", user["id"])


# ── 기초율 관리 ─────────────────────────────────────────────────────────────
BR_SOURCES = ["개발원2312", "경험률", "전년도보고서", "기타"]
BR_PERIODS = ["당기", "전기"]


def _actuary_base_rates(user):
    st.subheader("기초율 관리")
    st.caption("계리업의 핵심인 **경험기초율**(회사 경험으로 산출)과 **개발원 기초율**(3년 주기 표준)을 "
               "구분해 관리합니다. 각 산출이 어떤 기초율을 썼는지 추적하도록 세트(버전)로 보존합니다.")

    # 경험데이터를 올렸는데 아직 경험세트가 없는 회사 = 계리사 주의 대상
    _exp_stat = store.experience_upload_status(DB_PATH)
    _pending_exp = [x for x in _exp_stat if x["n_sets"] == 0]
    if _pending_exp:
        st.warning("🔔 **경험기초율 산출데이터를 올렸으나 아직 경험률을 산출하지 않은 기업**: "
                   + ", ".join(f"{x['company_name']}({x['last_upload'][:10]})" for x in _pending_exp)
                   + " — 아래 **경험 기초율** 코너에서 산출하세요.")

    corner = st.radio("코너", ["📈 경험 기초율", "📊 개발원 기초율"], horizontal=True, key="br_corner")
    if corner.startswith("📈"):
        _actuary_experience_rates(user, _exp_stat)
    else:
        _actuary_dev_rates(user)


def _actuary_experience_rates(user, exp_stat):
    """경험 기초율 코너 — 기업이 올린 경험데이터로 경험 퇴직률·승급률을 산출·저장."""
    st.markdown("#### 📈 경험 기초율 산출 (회사 경험 퇴직률·승급률)")
    st.caption("기업이 올린 '경험기초율 산출데이터'로 연령대별 경험 퇴직률·승급률을 산출합니다. "
               "사망률은 개발원 세트에서 차용합니다. 산출한 세트는 해당 기업 전용으로 저장됩니다.")
    if not exp_stat:
        st.info("아직 경험기초율 산출데이터를 올린 기업이 없습니다. "
                "기업이 '경험기초율 산출데이터'를 업로드하면 여기에 표시됩니다.")
        return

    # 회사 선택 (미산출 회사를 앞에 🔔로 표시)
    def _lbl(x):
        flag = "🔔 " if x["n_sets"] == 0 else "✅ "
        return f"{flag}{x['company_name']} (업로드 {x['n_uploads']} · 세트 {x['n_sets']})"
    opt = {x["company_id"]: x for x in exp_stat}
    cid = st.selectbox("기업 선택", list(opt), format_func=lambda i: _lbl(opt[i]), key="exp_co")
    files = store.list_aux_census(DB_PATH, cid, store.EXPERIENCE_CENSUS_TYPE)
    if not files:
        st.info("이 기업의 경험데이터 파일이 없습니다.")
        return
    fmap = {f["id"]: f for f in files}
    fid = st.selectbox("데이터 파일", list(fmap),
                       format_func=lambda i: f"{fmap[i]['filename']} ({fmap[i]['created'][:16]})",
                       key="exp_file")
    fpath = Path(fmap[fid]["stored_path"])
    if not fpath.exists():
        st.error("파일을 찾을 수 없습니다(서버에서 삭제됨).")
        return

    try:
        parsed = EXP.parse_experience_data(fpath.read_bytes())
    except Exception as e:  # noqa: BLE001
        st.error(f"경험데이터를 읽지 못했습니다: {e}")
        return

    c1, c2, c3 = st.columns(3)
    _os = parsed.get("obs_start")
    _oe = parsed.get("obs_end")
    obs_start = c1.date_input("관측 시작일", value=(_os or dt.date(dt.date.today().year - 3, 1, 1)),
                              key="exp_os")
    obs_end = c2.date_input("관측 종료일", value=(_oe or dt.date(dt.date.today().year - 1, 12, 31)),
                            key="exp_oe")
    ret_age = c3.number_input("정년", min_value=50, max_value=75, value=60, step=1, key="exp_ret")
    st.caption(f"인식된 종업원 {parsed['n']}명"
               + (f" · 파일 관측기간 {_os}~{_oe}" if _os and _oe else " · 관측기간은 위에서 지정"))

    # 사망률 차용 개발원 세트
    dev_sets = store.list_base_rate_sets(DB_PATH, kind="dev")
    _mopt = {0: "사망률 미적용(0)"}
    _mopt.update({s["id"]: f"[{s['id']}] {s['name']}" for s in dev_sets})
    mort_from = st.selectbox("사망률 차용(개발원 세트)", list(_mopt), format_func=lambda i: _mopt[i],
                             key="exp_mort", help="경험데이터로는 사망률을 추정하기 어려워 개발원 세트에서 가져옵니다.")

    if st.button("🧮 경험률 산출", type="primary", key="exp_run"):
        res = EXP.compute_experience_rates(parsed["records"], obs_start, obs_end, int(ret_age))
        st.session_state["exp_result"] = {"res": res, "cid": cid, "mort_from": mort_from,
                                          "ret": int(ret_age), "obs": f"{obs_start}~{obs_end}"}

    er = st.session_state.get("exp_result")
    if er and er.get("cid") == cid:
        res = er["res"]
        sm = res["summary"]
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("표본(명)", sm["n"])
        k2.metric("총 노출(인년)", f"{sm['exposure']:.1f}")
        k3.metric("경험 퇴직률(전체)", f"{(sm['overall_withdrawal'] or 0)*100:.2f}%")
        k4.metric("경험 승급률(평균)", f"{(sm['avg_raise'] or 0)*100:.2f}%")
        if sm["skipped"]:
            st.caption(f"생년월일·이력 부족 등으로 {sm['skipped']}건 제외.")
        st.markdown("**연령대(밴드)별 경험률**")
        st.dataframe(pd.DataFrame(res["bands"]), width="stretch", hide_index=True)

        rows = [dict(r) for r in res["rows"]]
        if er["mort_from"]:
            mfull = store.get_base_rate_set(DB_PATH, er["mort_from"])
            try:
                mrows = json.loads(mfull["data_json"]) if mfull and mfull.get("data_json") else []
            except Exception:  # noqa: BLE001
                mrows = []
            EXP.apply_mortality_from(rows, mrows)
        with st.expander("연령별 상세(기초율표 행) 미리보기", expanded=False):
            st.dataframe(_rates_display_df(rows), width="stretch", hide_index=True, height=280)

        cname = opt[cid]["company_name"]
        dname = st.text_input("세트 명칭", value=f"{cname} 경험 {er['obs'][:4]}", key="exp_name")
        if st.button("💾 경험 기초율 세트 저장", type="primary", key="exp_save"):
            store.add_base_rate_set(
                DB_PATH, dname.strip() or f"{cname} 경험", "경험률", er["obs"][:4], "당기",
                er["ret"], sm.get("avg_raise"),
                json.dumps(rows, ensure_ascii=False),
                f"경험데이터 산출 · 관측 {er['obs']} · 표본 {sm['n']}명 · 노출 {sm['exposure']}인년",
                user["id"], now(), company_id=cid, kind="experience")
            st.session_state.pop("exp_result", None)
            st.success(f"✅ '{dname}' 경험 기초율 세트를 저장했습니다. 산출 설계에서 선택할 수 있습니다.")
            st.rerun()

    # 이 회사의 기존 경험세트
    ex_sets = store.list_base_rate_sets(DB_PATH, kind="experience", company_id=cid)
    ex_sets = [s for s in ex_sets if s["company_id"] == cid]
    if ex_sets:
        st.markdown(f"**{opt[cid]['company_name']} 등록 경험세트 ({len(ex_sets)})**")
        st.dataframe(pd.DataFrame([{
            "ID": s["id"], "명칭": s["name"], "기준연도": s["base_year"], "정년": s["retirement_age"],
            "평균승급률(%)": round((s["avg_raise"] or 0) * 100, 2), "등록일": s["created"][:10],
        } for s in ex_sets]), width="stretch", hide_index=True)
        dsel = st.selectbox("경험세트 삭제", [0] + [s["id"] for s in ex_sets],
                            format_func=lambda i: "선택 안함" if i == 0 else f"[{i}]", key="exp_del_sel")
        if dsel and st.button("🗑 선택 경험세트 삭제", key="exp_del"):
            store.delete_base_rate_set(DB_PATH, dsel)
            st.rerun()


def _actuary_dev_rates(user):
    st.markdown("#### 📊 개발원 기초율 (300인 미만/이상 구분 · 3년 주기)")
    st.download_button("📄 기초율 입력 양식(템플릿) 내려받기", BR.build_base_rate_template(),
                       file_name="기초율_입력양식.xlsx", key="br_tmpl")
    st.caption("양식대로 채우면 깔끔하고, 다른 양식으로 올려도 **헤더(연령·퇴직률·사망률·승급률)를 자동 인식**합니다.")

    with st.expander("➕ 기초율표 업로드 → 새 세트 등록", expanded=False):
        up = st.file_uploader("기초율 양식 / PUC 워크북 / 기초율표 (xlsx·xlsm) — 헤더 자동 인식",
                              type=["xlsx", "xlsm"], key="br_up")
        parsed = None
        if up is not None:
            try:
                parsed = BR.parse_base_rate_table(bytes(up.getbuffer()))
                _kind = "개발원(300인 미만/이상 밴드)" if parsed.get("dev_format") else "표준(단일 퇴직·승급률)"
                st.success(f"[{_kind}] 연령 {len(parsed['rows'])}개 · 정년 {parsed['retirement_age']} · "
                           f"기준연도 {parsed.get('base_year') or '-'} 인식")
                if parsed.get("dev_format"):
                    st.caption("사업장 규모(300인 미만/이상)별 퇴직률·승급률을 함께 보존합니다. "
                               "산출 설계 단계에서 재직자수로 밴드를 선택합니다.")
                st.dataframe(_rates_display_df(parsed["rows"]),
                             width="stretch", hide_index=True, height=240)
            except Exception as e:  # noqa: BLE001
                st.error(f"기초율표를 읽지 못했습니다: {e}")
        c1, c2, c3 = st.columns(3)
        _def_src = 0 if not (parsed and parsed.get("dev_format")) else BR_SOURCES.index("개발원2312")
        name = c1.text_input("세트 명칭", placeholder="예: 2025 당기 (개발원2312)", key="br_name")
        source = c2.selectbox("출처", BR_SOURCES, index=_def_src, key="br_src")
        period = c3.selectbox("당기/전기", BR_PERIODS, key="br_period")
        base_year = c1.text_input("기준연도", value=(parsed.get("base_year") or "") if parsed else "",
                                  placeholder="예: 2025 / 2312", key="br_year")
        # 개발원 원본은 정년이 표에 없을 수 있어 직접 지정 가능
        ret_default = int(parsed["retirement_age"]) if (parsed and parsed.get("retirement_age")) else 60
        ret_in = c2.number_input("정년(세트 기본값)", min_value=50, max_value=75,
                                 value=ret_default, step=1, key="br_ret")
        note = st.text_input("메모", key="br_note")
        if st.button("💾 세트 저장", type="primary", key="br_save", disabled=(parsed is None)):
            if not name.strip():
                st.error("세트 명칭을 입력하세요.")
            else:
                store.add_base_rate_set(
                    DB_PATH, name.strip(), source, base_year, period,
                    int(ret_in), parsed.get("avg_raise"),
                    json.dumps(parsed["rows"], ensure_ascii=False), note, user["id"], now())
                st.success("기초율 세트를 저장했습니다.")
                st.rerun()

    sets = store.list_base_rate_sets(DB_PATH, kind="dev")
    st.markdown(f"**등록된 개발원 기초율 세트 ({len(sets)})**")
    if not sets:
        st.info("아직 등록된 세트가 없습니다. 위에서 기초율표를 업로드해 등록하세요.")
        return
    st.dataframe(pd.DataFrame([{
        "ID": s["id"], "명칭": s["name"], "출처": s["source"], "기준연도": s["base_year"],
        "당기/전기": s["period_kind"], "정년": s["retirement_age"],
        "평균승급률(%)": round((s["avg_raise"] or 0) * 100, 2), "등록일": s["created"][:10],
    } for s in sets]), width="stretch", hide_index=True)

    ids = [s["id"] for s in sets]
    labels = {s["id"]: f"[{s['id']}] {s['name']}" for s in sets}
    sel = st.selectbox("세트 상세보기", ids, format_func=lambda i: labels[i], key="br_view")
    if sel:
        full = store.get_base_rate_set(DB_PATH, sel)
        try:
            rows = json.loads(full["data_json"])
        except Exception:  # noqa: BLE001
            rows = []
        st.dataframe(_rates_display_df(rows),
                     width="stretch", hide_index=True, height=280)
        if st.button("🗑 이 세트 삭제", key="br_del"):
            store.delete_base_rate_set(DB_PATH, sel)
            st.rerun()


# ── 할인율 관리 ─────────────────────────────────────────────────────────────
DC_RATINGS = ["AA+", "AA", "AA-", "국공채", "기타"]


def _actuary_discount(user):
    st.subheader("할인율 관리")
    st.caption("만기별 할인율(회사채 spot 커브)을 입력하면 **부채 현금흐름에 대해 "
               "듀레이션이 반영된 단일할인율**을 산출합니다. "
               "(커브로 할인한 부채 PV = 단일율로 할인한 PV 가 되는 flat rate)")

    c1, c2, c3, c4 = st.columns(4)
    name = c1.text_input("커브 명칭", placeholder="예: 2025-12-31 AA+", key="dc_name")
    vdate = c2.text_input("기준일", placeholder="2025-12-31", key="dc_vdate")
    rating = c3.selectbox("등급", DC_RATINGS, key="dc_rating")
    period = c4.selectbox("당기/전기", BR_PERIODS, key="dc_period")
    timing_lbl = st.radio("할인 시점", TIMING_OPTS, horizontal=True, key="dc_timing")
    timing = "end_of_year" if "연말" in timing_lbl else "mid_year"

    st.markdown("**① 만기별 할인율(spot 커브)** — 만기(년)·할인율(예 0.03123)")
    curve_df = st.data_editor(
        pd.DataFrame({"만기": list(range(1, 21)), "할인율": [None] * 20}),
        num_rows="dynamic", width="stretch", key="dc_curve", height=280)

    st.markdown("**② 부채 예상지급액(현금흐름)** — 연도·예상지급액 "
                "(만기분석/엑셀에서 붙여넣기; 단일할인율은 현금흐름 '형태'로 결정)")
    cf_df = st.data_editor(
        pd.DataFrame({"연도": list(range(1, 21)), "예상지급액": [None] * 20}),
        num_rows="dynamic", width="stretch", key="dc_cf", height=280)

    def _curve_dict():
        out = {}
        for _, r in curve_df.iterrows():
            try:
                m = int(r["만기"]); v = float(r["할인율"])
                if v > 0:
                    out[m] = v
            except (TypeError, ValueError):
                continue
        return out

    def _cashflows():
        out = []
        for _, r in cf_df.iterrows():
            try:
                y = int(r["연도"]); a = float(r["예상지급액"])
                if a:
                    out.append((y, a))
            except (TypeError, ValueError):
                continue
        return out

    res = None
    if st.button("🧮 단일할인율 산출", type="primary", key="dc_solve"):
        curve = _curve_dict(); cf = _cashflows()
        if len(curve) < 2:
            st.error("만기별 할인율을 2개 이상 입력하세요.")
        elif len(cf) < 2:
            st.error("부채 예상지급액(현금흐름)을 2개 이상 입력하세요.")
        else:
            res = DISC.solve(cf, curve, timing)
            st.session_state["dc_res"] = res
            st.session_state["dc_res_curve"] = curve
    res = res or st.session_state.get("dc_res")
    if res:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("단일할인율", f"{res['single_rate']*100:.3f}%")
        m2.metric("듀레이션(년)", f"{res['duration']:.2f}")
        m3.metric("듀레이션 할인율", f"{res['duration_rate']*100:.3f}%")
        m4.metric("커브 부채 PV", f"{res['curve_pv']:,.0f}")
        if st.button("💾 커브·단일할인율 저장", key="dc_save"):
            store.add_discount_curve(
                DB_PATH, (name.strip() or f"{vdate} {rating}"), vdate, rating, period,
                json.dumps([{"maturity": m, "rate": v} for m, v in
                            sorted(st.session_state.get("dc_res_curve", {}).items())],
                           ensure_ascii=False),
                res["single_rate"], res["duration"], "", user["id"], now())
            st.success("할인율 커브를 저장했습니다.")
            st.rerun()

    curves = store.list_discount_curves(DB_PATH)
    st.markdown(f"**저장된 할인율 커브 ({len(curves)})**")
    if curves:
        st.dataframe(pd.DataFrame([{
            "ID": c["id"], "명칭": c["name"], "기준일": c["valuation_date"], "등급": c["rating"],
            "당기/전기": c["period_kind"],
            "단일할인율(%)": round((c["single_rate"] or 0) * 100, 3),
            "듀레이션": round(c["duration"] or 0, 2), "등록일": c["created"][:10],
        } for c in curves]), width="stretch", hide_index=True)
        dsel = st.selectbox("삭제할 커브", [c["id"] for c in curves],
                            format_func=lambda i: next(f"[{c['id']}] {c['name']}"
                                                       for c in curves if c["id"] == i),
                            key="dc_delsel")
        if st.button("🗑 커브 삭제", key="dc_del"):
            store.delete_discount_curve(DB_PATH, dsel)
            st.rerun()


def _actuary_edit_rows(user, sub, df, report, sid):
    """계리사용 오류행 직접 수정 — 화면에서 셀 고치고 저장하면 명부 갱신·재검증."""
    pv = problem_view(df, report, "all")
    if pv.empty:
        return
    with st.expander(f"✏️ 오류행 직접 수정 ({len(pv)}행) — 값 고치고 저장", expanded=False):
        st.caption("표에서 값을 고친 뒤 **수정본 저장**을 누르면 명부가 갱신되고 다시 검증합니다.")
        view = pv.drop(columns=["⚠문제"], errors="ignore")
        ren = {c: CR.STANDARD_KO.get(c, c) for c in view.columns}
        view = view.rename(columns=ren)
        edited = st.data_editor(view, hide_index=True, disabled=["엑셀행"],
                                width="stretch", key=f"rowedit_{sid}")
        if st.button("💾 수정본으로 저장 (명부 갱신)", key=f"roweditsave_{sid}", type="primary"):
            inv = {v: k for k, v in CR.STANDARD_KO.items()}
            full = df.copy()
            for _, r in edited.iterrows():
                try:
                    idx = int(r["엑셀행"]) - 2
                except (TypeError, ValueError):
                    continue
                if idx not in full.index:
                    continue
                for kcol in edited.columns:
                    if kcol == "엑셀행":
                        continue
                    ecol = inv.get(kcol, kcol)
                    if ecol in full.columns:
                        full.at[idx, ecol] = r[kcol]
            std_full = CR.standard_view(full)
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            newp = FILES_DIR / f"c{sub['company_id']}_active_{stamp}_edited.xlsx"
            std_full.to_excel(newp, index=False)
            vdate = dt.date.fromisoformat(sub["valuation_date"])
            recs2, rep2, _ = load_census(newp, column_map=COLMAP)
            validate_census(recs2, vdate, rep2)
            run_smart_checks(recs2, vdate, rep2)
            _rg, _eg = _retirement_ages(sub["company_id"])
            run_actuary_checks(recs2, vdate, rep2, retirement_age=_rg, exec_retirement_age=_eg)
            _run_aux_cross(sub["company_id"], recs2, rep2)
            store.update_submission_file(DB_PATH, sid, newp.name, str(newp), rep2.n_records,
                                         len(rep2.errors), len(rep2.warnings), now())
            store.log_action(DB_PATH, user["id"], "edit_rows", now(), f"submission#{sid}")
            st.success(f"✅ 수정본 저장 — 오류 {len(rep2.errors)}건 · 경고 {len(rep2.warnings)}건")
            st.session_state.pop(f"anal_{sid}", None)
            st.rerun()


def _actuary_census_view(user, sub, sid):
    """명부조회: 산출 기본정보(인원·추계액·구분별)·오류현황·다운로드·수정 재업로드."""
    from collections import Counter
    mp = Path(sub["stored_path"])
    st.markdown(f"**재직자명부**: {sub['filename']} · {sub['n_records']}명 "
                f"(오류 {sub['n_errors']} · 경고 {sub['n_warnings']})")
    if mp.exists():
        st.download_button("⬇ 재직자명부 내려받기(수정용)", mp.read_bytes(),
                           file_name=mp.name, key=f"cend_{sid}")
        if st.button("🔍 산출 기본정보·오류 상세 분석", key=f"anal_{sid}"):
            st.session_state[f"anal_{sid}"] = True
        if st.session_state.get(f"anal_{sid}"):
            try:
                vdate = dt.date.fromisoformat(sub["valuation_date"])
                recs, rep, df = load_census(mp, column_map=COLMAP)
                validate_census(recs, vdate, rep)
                run_smart_checks(recs, vdate, rep)
                _rg, _eg = _retirement_ages(sub["company_id"])
                run_actuary_checks(recs, vdate, rep,
                                   retirement_age=_rg, exec_retirement_age=_eg)
                _has_aux = _run_aux_cross(sub["company_id"], recs, rep)
                if _has_aux:
                    st.caption("🔗 보조명부(퇴직자·전출입)·전기말재직자명부 교차검증을 포함했습니다.")
                accr = sum(float(getattr(r, "current_year_accrual", 0) or 0) for r in recs)
                by_cls = Counter(str(getattr(r, "emp_class", "")) for r in recs)
                k1, k2, k3 = st.columns(3)
                k1.metric("인원(재직)", f"{rep.n_records}명")
                k2.metric("당년도추계액 합계", eok(accr))
                k3.metric("오류·경고", f"{len(rep.errors)}·{len(rep.warnings)}")
                if by_cls:
                    st.caption("구분별 인원: " + ", ".join(f"{k}={v}" for k, v in by_cls.items()))
                try:
                    raw_df = pd.read_excel(mp) if str(mp).lower().endswith((".xlsx", ".xls")) \
                        else pd.read_csv(mp)
                except Exception:  # noqa: BLE001
                    raw_df = None
                _error_review_ui(df, rep, f"act_{sid}", raw_df=raw_df,
                                 summary={"filename": sub["filename"],
                                          "valuation_date": sub["valuation_date"]})
                _actuary_edit_rows(user, sub, df, rep, sid)
            except Exception as e:  # noqa: BLE001
                st.warning(f"명부 분석 실패: {e}")
    else:
        st.warning("명부 파일을 찾을 수 없습니다.")

    st.markdown("**🔄 수정한 명부 재업로드 (계리사 직접 갱신)**")
    reup = st.file_uploader("수정 명부 업로드 (xlsx/csv)", type=["xlsx", "csv"], key=f"reup_{sid}")
    if reup is not None and st.button("갱신 저장", key=f"reupsave_{sid}"):
        raw = bytes(reup.getbuffer())
        FILES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        newpath = FILES_DIR / f"c{sub['company_id']}_reup_{stamp}_{reup.name}"
        dropped, err = _save_census_file(raw, reup.name, newpath)
        if err:
            st.error(f"파일 오류: {err}")
        else:
            vdate = dt.date.fromisoformat(sub["valuation_date"])
            recs, rep, _ = load_census(newpath, column_map=COLMAP)
            validate_census(recs, vdate, rep)
            run_smart_checks(recs, vdate, rep)
            run_actuary_checks(recs, vdate, rep)
            store.update_submission_file(DB_PATH, sid, reup.name, str(newpath),
                                         rep.n_records, len(rep.errors), len(rep.warnings), now())
            store.log_action(DB_PATH, user["id"], "census_update", now(), f"submission#{sid}")
            if dropped:
                st.info(f"🔒 개인정보 컬럼 자동 삭제: {', '.join(dropped)}")
            st.success(f"명부를 갱신했습니다. ({rep.n_records}명, 오류 {len(rep.errors)}·경고 {len(rep.warnings)})")
            st.rerun()

    # 보조 명부 + 기타장기 규정
    aux = [a for a in store.list_aux_census(DB_PATH, sub["company_id"])
           if a["census_type"] != "누진제규정서류"]
    if aux:
        st.markdown("**보조 명부 (퇴직자·전기말·전출입·기타장기 등)**")
        for f in aux:
            p = Path(f["stored_path"])
            if p.exists():
                st.download_button(f"⬇ [{f['census_type']}] {f['filename']}", p.read_bytes(),
                                   file_name=f["filename"], key=f"auxa_{f['id']}")
    # 사외적립자산·기타장기·명부확인용요약표 — 계리사도 동일 양식으로 조회·작성 가능
    cid = sub["company_id"]
    with st.expander("💰 사외적립자산 (양식 다운로드·업로드·편집)", expanded=False):
        _client_operation_status(user, company_id=cid, kp="act_")
    with st.expander("🎖 기타장기 (양식 다운로드·업로드·편집)", expanded=False):
        _client_other_lt(user, company_id=cid, kp="act_")
    with st.expander("📑 명부확인용 요약표 (재직자명부 자동산출)", expanded=False):
        _client_census_summary(user, company_id=cid, kp="act_")


def _actuary_work_detail(user, sid):
    sub = store.get_submission(DB_PATH, sid)
    st.subheader(f"2. [신청번호 {_apply_no(sub)}] {sub['company_name']} · 산출기준일 {sub['valuation_date']}")

    # 접수여부 제어 + 견적액/비고
    with st.container(border=True):
        h1, h2, h3, h4 = st.columns([2, 1, 1, 1])
        h1.markdown(f"접수여부: **{store.acceptance_of(sub['status'])}**")
        if sub["status"] == "submitted":
            if h2.button("📥 접수", key=f"accept_{sid}",
                         help="접수하면 이후에는 계리사만 수정할 수 있습니다(기업 수정·삭제 불가)."):
                store.update_submission_status(DB_PATH, sid, "accepted", now())
                store.stamp_stage_time(DB_PATH, sid, "accepted_at", now())
                store.log_action(DB_PATH, user["id"], "accept", now(), f"submission#{sid}")
                st.rerun()
        elif sub["status"] == "on_hold":
            if h2.button("▶ 보류해제", key=f"resume_{sid}"):
                store.update_submission_status(DB_PATH, sid, "accepted", now()); st.rerun()
        elif sub["status"] in ("accepted", "calculated"):
            if h2.button("⏸ 보류", key=f"hold_{sid}"):
                store.update_submission_status(DB_PATH, sid, "on_hold", now()); st.rerun()
        if store.can_actuary_delete(sub["status"]):
            if h3.button("🗑 삭제", key=f"wd_del_{sid}", help="이 신청건 삭제(확인은 목록에서)"):
                st.session_state["actuary_del_sid"] = sid
                st.rerun()
        if sub["status"] in store.CLIENT_EDITABLE:
            h4.caption("신청 단계 (기업·계리사 수정/삭제 가능)")
        elif sub["status"] == "reported":
            h4.caption("🔒 보고완료")
        else:
            h4.caption("접수됨 (계리사만 수정)")
        q1, q2, q3 = st.columns([1, 1, 2])
        quote = q1.number_input("견적액(만원)", value=float(sub["quote_amount"] or 0.0),
                                step=10.0, key=f"quote_{sid}")
        try:
            _pd = dt.date.fromisoformat(sub["promised_date"]) if sub["promised_date"] else None
        except Exception:
            _pd = None
        promised = q2.date_input("약속일(보고 예정)", value=_pd, key=f"pdate_{sid}")
        memo = q3.text_input("비고", value=sub["note"] or "", key=f"memo_{sid}")
        if st.button("💾 견적액·약속일·비고 저장", key=f"savemeta_{sid}"):
            store.set_submission_meta(DB_PATH, sid, quote_amount=quote,
                                      promised_date=(promised.isoformat() if promised else ""),
                                      note=memo)
            st.success("저장했습니다."); st.rerun()

    # 조회 4종 — 제도·운영현황은 기업화면과 동일 내용, 명부조회는 산출 기본정보·오류·재업로드
    with st.expander("📋 제도조회 (기업 입력내용)", expanded=False):
        _plan_readonly(sub["company_id"])
    with st.expander("📊 운영현황조회 (사외적립자산 현황)", expanded=False):
        _funding_readonly(sub["company_id"])
    with st.expander("📁 명부조회 (산출 기본정보·오류·재업로드)", expanded=False):
        _actuary_census_view(user, sub, sid)
    with st.expander("📎 지난보고서조회", expanded=False):
        pr = store.get_prior_record(DB_PATH, sub["company_id"])
        pfiles = store.list_prior_files(DB_PATH, sub["company_id"])
        if pr:
            st.write({k: pr[k] for k in store.PRIOR_FIELDS if pr.get(k) not in (None, "", 0.0)})
        for f in pfiles:
            p = Path(f["stored_path"])
            if p.exists():
                st.download_button(f"⬇ {f['filename']}", p.read_bytes(),
                                   file_name=f["filename"], key=f"pf_{f['id']}")
        if not pr and not pfiles:
            st.info("등록된 과거 자료가 없습니다.")

    st.divider()
    st.markdown("#### 가정설계")
    c1, c2, c3, c4 = st.columns(4)
    val_date = c1.date_input("산출기준일", dt.date.fromisoformat(sub["valuation_date"]))
    discount = c2.number_input("할인율 (%)", value=4.5, step=0.1) / 100
    salary = c3.number_input("임금상승율 (%)", value=3.0, step=0.1) / 100
    ret_age = c4.number_input("정년", value=60, step=1)
    _tcur = _plan_timing(sub["company_id"])
    timing_lbl = st.radio("탈퇴·지급 시점 (할인 기준)", TIMING_OPTS,
                          index=(1 if _tcur == "end_of_year" else 0), horizontal=True,
                          key=f"timing_{sid}",
                          help="연중(mid-year): 연중 탈퇴·지급 가정, 반기 할인 — 한국 실무 표준·기본값. "
                               "/ 연말(end-of-year): 연말 시점 할인.")
    run_timing = "end_of_year" if "연말" in timing_lbl else "mid_year"

    # 기초율 세트·할인율 커브 선택 (마스터 데이터 연동) — 이 회사 경험세트 + 공용 개발원세트
    _brsets = store.list_base_rate_sets(DB_PATH, company_id=sub["company_id"])
    _dccurves = store.list_discount_curves(DB_PATH)
    bcol, dcol = st.columns(2)
    # 적용 기초율 = (세트 + 300인 밴드)를 하나의 선택지로. 재직자수로 밴드 자동 세팅.
    _nrec = sub.get("n_records") or 0
    _auto_band = "ge300" if _nrec >= 300 else "lt300"
    _br_opts = [{"key": "base:lt300", "label": "기본(CSV 표준 테이블)", "set_id": 0, "band": "lt300"}]
    for s in _brsets:
        if s.get("kind") == "experience":
            _br_opts.append({"key": f"{s['id']}:exp", "label": f"📈 {s['name']} · 경험률",
                             "set_id": s["id"], "band": "lt300"})
            continue
        _full = store.get_base_rate_set(DB_PATH, s["id"])
        try:
            _rows = json.loads(_full["data_json"]) if _full and _full.get("data_json") else []
        except Exception:  # noqa: BLE001
            _rows = []
        if _is_dev_set(_rows):
            _br_opts.append({"key": f"{s['id']}:lt300", "label": f"{s['name']} · 300인 미만",
                             "set_id": s["id"], "band": "lt300"})
            _br_opts.append({"key": f"{s['id']}:ge300", "label": f"{s['name']} · 300인 이상",
                             "set_id": s["id"], "band": "ge300"})
        else:
            _br_opts.append({"key": f"{s['id']}:single", "label": s["name"],
                             "set_id": s["id"], "band": "lt300"})
    # 디폴트: 이 회사 경험세트가 있으면 그것(경험율 우선 선택), 없으면 최신 개발원세트 + 재직자수 밴드
    _keys = [o["key"] for o in _br_opts]
    _exp_first = next((s["id"] for s in _brsets if s.get("kind") == "experience"), None)
    _dev_first = next((s["id"] for s in _brsets if s.get("kind") != "experience"), None)
    if _exp_first is not None:
        _default_key = f"{_exp_first}:exp"
    elif _dev_first is not None:
        _cand = f"{_dev_first}:{_auto_band}"
        _default_key = _cand if _cand in _keys else next(
            (k for k in _keys if k.startswith(f"{_dev_first}:")), _br_opts[0]["key"])
    else:
        _default_key = _br_opts[0]["key"]
    _optmap = {o["key"]: o for o in _br_opts}
    _has_exp = _exp_first is not None
    _sel_key = bcol.selectbox(
        "적용 기초율(세트 · 사업장규모)", _keys, index=_keys.index(_default_key),
        format_func=lambda k: _optmap[k]["label"], key=f"brsel_{sid}",
        help=("이 기업의 경험 기초율이 있어 **경험율이 기본 선택**됩니다. " if _has_exp else
              f"재직자수({_nrec}명) 기준 300인 {'이상' if _nrec >= 300 else '미만'} 밴드를 기본 선택합니다. ")
             + "세트·규모를 바꿔 선택할 수 있습니다.")
    if _has_exp:
        bcol.caption("📈 이 기업의 경험 기초율이 등록되어 있어 기본으로 선택되었습니다.")
    base_set_id = _optmap[_sel_key]["set_id"]
    size_band = _optmap[_sel_key]["band"]
    _dcopts = {0: "직접입력(위 할인율 사용)"}
    _dcopts.update({c["id"]: f"[{c['id']}] {c['name']} · 단일 {(c['single_rate'] or 0)*100:.3f}%"
                    for c in _dccurves})
    curve_id = dcol.selectbox("할인율 커브", list(_dcopts), format_func=lambda i: _dcopts[i],
                              key=f"dcsel_{sid}",
                              help="커브를 선택하면 이 명부의 현금흐름으로 듀레이션 반영 단일할인율을 자동 산출해 적용합니다.")

    # 주석공시 조정내역용 회사 재무자료 (사외적립자산·기여금·전기값·재측정손익)
    _dsv = store.get_disclosure_inputs(DB_PATH, sid) or {}
    with st.expander("💰 회사 재무자료 입력 (주석공시 조정내역용)", expanded=False):
        st.caption("보고서 Ⅱ.주석공시사항의 사외적립자산·확정급여채무 조정내역·재측정손익 표에 반영됩니다. "
                   "미입력 시 해당 항목은 0으로 표시됩니다(당기 계산값은 자동 반영).")
        f1, f2, f3 = st.columns(3)
        di_in = {}
        di_in["dbo_begin"] = f1.number_input("기초 확정급여채무", value=float(_dsv.get("dbo_begin") or 0.0), step=1e6, format="%.0f")
        di_in["plan_assets_begin"] = f2.number_input("기초 사외적립자산", value=float(_dsv.get("plan_assets_begin") or 0.0), step=1e6, format="%.0f")
        di_in["plan_assets"] = f3.number_input("기말 사외적립자산", value=float(_dsv.get("plan_assets") or 0.0), step=1e6, format="%.0f")
        di_in["interest_income"] = f1.number_input("사외적립자산 이자수익", value=float(_dsv.get("interest_income") or 0.0), step=1e6, format="%.0f")
        di_in["contributions"] = f2.number_input("기여금 납부액", value=float(_dsv.get("contributions") or 0.0), step=1e6, format="%.0f")
        di_in["asset_return"] = f3.number_input("사외적립자산 수익(순이자 제외)", value=float(_dsv.get("asset_return") or 0.0), step=1e6, format="%.0f")
        di_in["benefits_paid"] = f1.number_input("급여지급액(사외적립자산)", value=float(_dsv.get("benefits_paid") or 0.0), step=1e6, format="%.0f")
        di_in["benefits_paid_dbo"] = f2.number_input("급여지급액(확정급여채무)", value=float(_dsv.get("benefits_paid_dbo") or 0.0), step=1e6, format="%.0f")
        di_in["net_interest"] = f3.number_input("순확정급여부채 순이자", value=float(_dsv.get("net_interest") or 0.0), step=1e6, format="%.0f")
        di_in["remeasure_demographic"] = f1.number_input("재측정손익-인구통계적가정", value=float(_dsv.get("remeasure_demographic") or 0.0), step=1e6, format="%.0f")
        di_in["remeasure_financial"] = f2.number_input("재측정손익-재무적가정", value=float(_dsv.get("remeasure_financial") or 0.0), step=1e6, format="%.0f")
        di_in["remeasure_experience"] = f3.number_input("재측정손익-가정과 실제 차이", value=float(_dsv.get("remeasure_experience") or 0.0), step=1e6, format="%.0f")
        di_in["npc_conversion"] = f1.number_input("국민연금전환금", value=float(_dsv.get("npc_conversion") or 0.0), step=1e6, format="%.0f")
        if st.button("💾 재무자료 저장"):
            store.save_disclosure_inputs(DB_PATH, sid, di_in, user["id"], now())
            st.success("재무자료를 저장했습니다.")
            st.rerun()

    st.caption("가정을 바꿔가며 **여러 번 계산(시뮬레이션)**할 수 있습니다. "
               "결과가 확정되면 아래 **보고서 확정**을 눌러야 고객이 조회·메일 수신할 수 있습니다.")
    # 재계산 시 검토상태 초기화 안내 — 방금 리셋되었으면 메시지, 검토단계면 사전 경고
    if st.session_state.pop(f"review_reset_{sid}", False):
        st.warning("🔁 기업고객이 **검토완료(또는 검토요청)** 한 건이었습니다. 데이터·가정이 바뀌어 "
                   "**검토완료가 해제**되고 **계산완료** 단계로 돌아갔습니다. 기업이 바뀐 내용을 다시 "
                   "확인하도록 아래에서 **‘🔎 기업검토 요청’을 다시 신청**하세요.")
    _was_reviewed = sub["status"] == "client_review"
    _was_confirmed = bool(sub.get("client_confirmed_at"))
    if _was_reviewed:
        st.warning("⚠️ 이 건은 **기업검토요청**된 건입니다"
                   + ("(기업 **검토완료**됨). " if _was_confirmed else ". ")
                   + "다시 계산 실행하면 **검토완료가 해제**되고 계산완료로 돌아가, "
                   "이후 **‘🔎 기업검토 요청’을 다시 신청**해야 합니다.")
    rc1, rc2 = st.columns([1, 2])
    run = rc1.button("🟨 계산 실행 (시뮬레이션)", type="primary", key=f"run_{sid}")
    force = rc2.checkbox("오류가 있어도 이대로 계산 (유효한 레코드만 사용)", key=f"force_{sid}")
    if run:
        _timing = run_timing
        records, report, _ = load_census(sub["stored_path"], column_map=COLMAP)

        def _mkcfg(rate):
            return Config.from_dict({
                "valuation_date": val_date.isoformat(), "discount_rate": rate,
                "salary_increase_rate": salary, "retirement_age": ret_age,
                "decrement_timing": _timing, "discount_timing": _timing,
            })

        # 기초율: 선택 세트 → DecrementTables (미선택 시 CSV 표준). 300인 밴드 반영.
        if base_set_id:
            tables = _tables_from_base_set(base_set_id, size_band)
        else:
            tables = DecrementTables.from_config(_mkcfg(discount), base_dir=str(CONFIG_DIR))

        # 할인율: 커브 선택 시 이 명부 현금흐름으로 단일할인율 자동 산출
        disc_rate = discount
        if curve_id:
            from dbo.engine import expected_cashflows
            crow = store.get_discount_curve(DB_PATH, curve_id)
            try:
                curve = {int(x["maturity"]): float(x["rate"])
                         for x in json.loads(crow["curve_json"])}
            except Exception:  # noqa: BLE001
                curve = {}
            cfdf = expected_cashflows(records, _mkcfg(discount), tables)
            cflist = [(int(r["연도"]), float(r["기대급여지급액"]))
                      for _, r in cfdf.iterrows() if r["기대급여지급액"]]
            if len(curve) >= 2 and len(cflist) >= 2:
                disc_rate = DISC.single_equivalent_rate(cflist, curve, _timing)
                st.info(f"할인율 커브 '{crow['name']}' 적용 → 듀레이션 반영 **단일할인율 "
                        f"{disc_rate*100:.3f}%** 자동 산출 (직접입력 할인율 대신 사용)")
            else:
                st.warning("커브 또는 현금흐름이 부족해 직접입력 할인율을 사용합니다.")

        config = _mkcfg(disc_rate)
        validate_census(records, config.valuation_date, report)
        run_smart_checks(records, config.valuation_date, report)
        run_actuary_checks(records, config.valuation_date, report, config=config)
        _run_aux_cross(sub["company_id"], records, report)
        if report.has_errors and not force:
            st.error(f"명부에 오류 {len(report.errors)}건이 있습니다. 내용을 확인한 뒤 "
                     "위 '오류가 있어도 이대로 계산'을 체크하면 진행할 수 있습니다. (또는 기업에 수정 요청)")
            for i in report.errors[:30]:
                who = f" (사번 {i.emp_id})" if i.emp_id else ""
                st.markdown(f"- {i.message}{who}")
        else:
            if report.has_errors:
                st.warning(f"⚠️ 오류 {len(report.errors)}건을 무시하고 유효한 {len(records)}명 기준으로 계산합니다.")
            result = calculate_census(records, config, tables, with_detail=False)
            out_dir = FILES_DIR / "results" / f"sub_{sid}"
            disc_in = store.get_disclosure_inputs(DB_PATH, sid)
            if disc_in:
                # 전기말 열은 기초값으로 표기(기초 = 전기말 잔액)
                disc_in = dict(disc_in)
                disc_in.setdefault("dbo_prior", disc_in.get("dbo_begin"))
                disc_in.setdefault("plan_assets_prior", disc_in.get("plan_assets_begin"))
            paths = write_outputs(out_dir, records, result, config, tables,
                                  census_path=sub["stored_path"], report=report, timestamp=now(),
                                  company=sub["company_name"],
                                  plan_info=store.get_plan_info(DB_PATH, sub["company_id"]),
                                  prior=store.get_prior_record(DB_PATH, sub["company_id"]),
                                  disclosure_inputs=disc_in)
            rid = store.save_result(DB_PATH, sid, user["id"], result.total_dbo, result.total_csc,
                                    len(result.results), len(result.excluded_emp_ids),
                                    str(paths["xlsx"]), str(paths["run_log"]), now())
            # 사용한 기초율 세트·할인율 커브 기록 (감사추적)
            store.set_result_rate_refs(DB_PATH, rid, base_set_id or None, curve_id or None)
            # 산출결과 부가지표(듀레이션·민감도·전기PBO) 저장 + 사용 기초율 스냅샷 보존
            _metrics = _compute_result_metrics(records, result, config, tables,
                                               ret_age, sub["company_id"])
            if base_set_id:
                _metrics["base_rates"] = _base_set_snapshot(base_set_id, size_band)
            store.save_result_metrics(DB_PATH, rid, _metrics)
            _ver = store.mark_calculated(DB_PATH, sid, now())
            # 검토요청/검토완료 건을 재계산하면 검토상태가 초기화됨 → 다음 렌더에서 안내
            if _was_reviewed or _was_confirmed:
                st.session_state[f"review_reset_{sid}"] = True
            store.log_action(DB_PATH, user["id"], "calculate", now(), f"submission#{sid} v{_ver}")
            st.rerun()

    res = store.latest_result(DB_PATH, sid)
    if res:
        st.divider()
        st.markdown("#### 산출결과")
        _render_result_blocks(res)
        xlsx = Path(res["xlsx_path"])
        rep_xlsx = xlsx.parent / "dbo_report.xlsx"
        rep_pptx = xlsx.parent / "dbo_report.pptx"
        dc1, dc2, dc3 = st.columns(3)
        if rep_pptx.exists():
            dc1.download_button("📊 계리평가보고서 (파워포인트)", rep_pptx.read_bytes(),
                                file_name=f"{sub['company_name']}_계리평가보고서.pptx", width="stretch")
        if rep_xlsx.exists():
            dc2.download_button("📑 계리평가보고서 (엑셀 전문서식)", rep_xlsx.read_bytes(),
                                file_name=f"{sub['company_name']}_계리평가보고서.xlsx", width="stretch")
        if xlsx.exists():
            dc3.download_button("📄 상세 산출물 (개인별·요약)", xlsx.read_bytes(),
                                file_name=f"{sub['company_name']}_상세산출물.xlsx", width="stretch")

        _report_email_ui(user, sub, rep_pptx, rep_xlsx)

        st.divider()
        # ── 단계: 계산완료 → 기업검토요청 → (기업 확인) → 보고완료 ──
        _cv = sub.get("calc_version")
        _cat = (sub.get("calculated_at") or "")[:16]
        st.markdown(f"**진행단계:** {store.STATUS_LABELS.get(sub['status'], sub['status'])}"
                    + (f"  ·  계산완료 **v{_cv}** ({_cat})" if _cv else ""))
        if sub["status"] == "calculated":
            st.caption("계산이 끝났습니다. 기업 검토를 요청하면 기업이 결과를 확인합니다.")
            if st.button("🔎 기업검토 요청", type="primary", key=f"reqreview_{sid}"):
                store.update_submission_status(DB_PATH, sid, "client_review", now())
                store.stamp_stage_time(DB_PATH, sid, "review_requested_at", now())
                store.log_action(DB_PATH, user["id"], "request_review", now(), f"submission#{sid}")
                st.rerun()
        elif sub["status"] == "client_review":
            cc = (sub.get("client_confirmed_at") or "")[:16]
            if cc:
                st.success(f"기업이 검토 확인했습니다 ({cc}). 최종 보고완료할 수 있습니다.")
                if st.button("✅ 최종 보고완료", type="primary", key=f"finalreport_{sid}"):
                    store.mark_reported(DB_PATH, sid, now())
                    dest = _archive_completed_report(sub, res)
                    store.log_action(DB_PATH, user["id"], "report", now(), f"submission#{sid} → {dest}")
                    st.rerun()
            else:
                st.info("기업 검토 확인 대기중입니다. (기업 화면에서 '검토 확인'을 눌러야 최종 보고완료 가능)")
            if st.button("↩ 검토요청 취소(계산완료로)", key=f"cancelreview_{sid}"):
                store.update_submission_status(DB_PATH, sid, "calculated", now()); st.rerun()
        elif sub["status"] == "reported":
            _rat = (sub.get("reported_at") or "")[:16]
            st.success(f"✅ **보고완료** (보고일시 {_rat} · 계산 v{_cv}) — 고객이 조회·이메일 수신 가능. "
                       f"완료보고서 폴더: 산출일 {sub['valuation_date']}")
            if st.button("✏️ 보고완료 해제(재시뮬레이션)", key=f"unreport_{sid}",
                         help="해제하고 가정을 바꿔 다시 계산할 수 있습니다."):
                store.update_submission_status(DB_PATH, sid, "calculated", now())
                store.log_action(DB_PATH, user["id"], "unreport", now(), f"submission#{sid}")
                st.rerun()

    # 감사 대응 Q&A (표준 답변·근거 자동생성) — 계리사는 코멘트 작성 가능
    st.divider()
    _render_audit_qa(sub, f"a_{sid}", editable=True)


# ---------------------------------------------------------------------------
# 관리자 (admin)
# ---------------------------------------------------------------------------

def _completed_reports_browser():
    """산출일 기준으로 정리된 완료 보고서 폴더를 탐색·다운로드."""
    st.subheader("📁 완료 보고서 (산출일 기준 폴더)")
    q = st.text_input("🔍 회사 검색", key="done_search", placeholder="회사명 일부 입력")
    date_dirs = sorted([d for d in ARCHIVE_DIR.iterdir() if d.is_dir()], reverse=True) \
        if ARCHIVE_DIR.exists() else []
    if not date_dirs:
        st.info("아직 확정된 완료 보고서가 없습니다. 계리사가 '보고서 확정'을 하면 여기에 산출일별로 정리됩니다.")
        return
    qs = (q or "").strip().lower()
    shown = 0
    for dd in date_dirs:
        companies = sorted([c for c in dd.iterdir() if c.is_dir()
                            and (not qs or qs in c.name.lower())])
        if not companies:
            continue
        shown += len(companies)
        with st.expander(f"📅 산출일 {dd.name}  ·  {len(companies)}건", expanded=bool(qs)):
            for c in companies:
                st.markdown(f"**{c.name}**")
                files = sorted([f for f in c.iterdir() if f.is_file()])
                fcols = st.columns(3)
                for i, f in enumerate(files):
                    fcols[i % 3].download_button(
                        f"⬇ {f.name}", f.read_bytes(), file_name=f.name,
                        key=f"arch_{dd.name}_{c.name}_{f.name}", width="stretch")
    if shown == 0:
        st.info("조건에 맞는 완료 보고서가 없습니다.")


def page_admin(user):
    st.markdown("## 🛠 관리자 전용")
    counts = store.status_counts(DB_PATH)
    in_progress = sum(counts.get(s, 0) for s in ("submitted", "accepted", "calculated", "on_hold", "client_review"))
    qa_pending = sum(store.qa_pending_counts(DB_PATH).values())
    m1, m2 = st.columns(2)
    m1.metric("진행중", f"{in_progress}건")
    m2.metric("질의응답 대기", f"{qa_pending}건")

    t1, t2, t3, t4, t5, t6, t7 = st.tabs(
        ["🏢 기업조회", "💬 질의응답현황", "💰 매출현황", "➕ 기업 신규등록",
         "🏢 기업정보 조회/수정", "📁 완료 보고서", "🧹 데이터 초기화"])
    with t1:
        _admin_company_lookup(user)
    with t2:
        _admin_qa_status()
    with t3:
        _admin_revenue()
    with t4:
        _admin_new_company(user)
    with t5:
        _admin_company_info(user)
    with t6:
        _completed_reports_browser()
    with t7:
        _admin_reset(user)


def _admin_reset(user):
    st.subheader("데이터 초기화")
    st.error("⚠️ 등록된 데이터를 **영구 삭제**합니다. 되돌릴 수 없습니다. (계리사·관리자 계정은 유지)")
    companies = store.list_companies(DB_PATH)
    subs = store.list_submissions(DB_PATH)
    st.markdown(f"- 등록 기업: **{len(companies)}개**  ·  신청/산출 건: **{len(subs)}건**")

    scope = st.radio(
        "삭제 범위",
        ["업무데이터만 (신청·산출·제도·기초율 등 · 기업/계정 유지)",
         "전체 (등록한 기업·기업담당자 계정까지 삭제)"],
        key="reset_scope")
    wipe_companies = scope.startswith("전체")

    confirm = st.text_input('확인을 위해 **초기화** 라고 입력하세요', key="reset_confirm",
                            placeholder="초기화")
    if st.button("🧹 데이터 초기화 실행", type="primary",
                 disabled=(confirm.strip() != "초기화")):
        removed = store.reset_platform_data(DB_PATH, wipe_companies=wipe_companies)
        # 업로드 파일도 정리
        try:
            if FILES_DIR.exists():
                shutil.rmtree(FILES_DIR)
            FILES_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        store.log_action(DB_PATH, user["id"], "reset_data", now(),
                         f"wipe_companies={wipe_companies}")
        n = sum(v for v in removed.values())
        st.success(f"초기화 완료 — 총 {n}건 삭제. 화면을 새로고침하면 반영됩니다.")
        for k in list(st.session_state.keys()):
            if k.endswith("_sel_sid") or k.endswith("_detail_sid") or k.endswith("_del_sid"):
                st.session_state.pop(k, None)
        st.rerun()


def _admin_company_lookup(user):
    st.subheader("1. 기업조회")
    st.caption("기업명 / 산출기준일 / 산출자 기준으로 조회합니다.")
    subs = [s for s in store.list_submissions(DB_PATH)
            if s["status"] not in ("needs_fix", "validated")]
    if not subs:
        st.info("아직 신청된 건이 없습니다.")
        return
    f1, f2, f3 = st.columns(3)
    qn = f1.text_input("🔍 기업명", key="adm_name").strip().lower()
    vdates = ["전체"] + sorted({s["valuation_date"] for s in subs}, reverse=True)
    qv = f2.selectbox("산출기준일", vdates, key="adm_vdate")
    calcs = ["전체"] + sorted({(s["calculator"] or "-") for s in subs})
    qc = f3.selectbox("산출자", calcs, key="adm_calc")
    rows = [s for s in subs
            if (not qn or qn in (s["company_name"] or "").lower())
            and (qv == "전체" or s["valuation_date"] == qv)
            and (qc == "전체" or (s["calculator"] or "-") == qc)]
    if not rows:
        st.info("조건에 맞는 기업이 없습니다.")
        return

    table = []
    for s in rows:
        res = store.latest_result(DB_PATH, s["id"])
        done = (res["created"][:10] if (res and s["status"] in ("calculated", "reported"))
                else ("산출중" if s["status"] in ("submitted", "accepted", "on_hold", "client_review") else "-"))
        table.append({
            "기업명": s["company_name"], "산출기준일": s["valuation_date"],
            "신청일": (s["created"] or "")[:10], "접수여부": store.acceptance_of(s["status"]),
            "약속일": s["promised_date"] or "-", "산출완료일": done,
            "산출목적": s["purpose"] or "IFRS-1019",
            "보고서": ("확정" if s["status"] == "reported" else "계산완료"
                     if s["status"] == "calculated" else "-" if s["status"] == "cancelled" else "산출중"),
            "산출자": s["calculator"] or "-", "신청자": s["applicant"] or "-",
            "견적발송": s["quote_sent"] or "미발송",
            "견적액": s["quote_amount"] if s["quote_amount"] is not None else None,
            "계약서발송": s["contract_sent"] or "미발송",
            "수금상황": s["collection_status"] or "미청구",
            "비고": s["note"] or "",
        })
    st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True)

    st.divider()
    st.markdown("##### 영업정보 입력/수정")
    label = {s["id"]: f"{s['company_name']} · {s['valuation_date']} "
                       f"[{store.acceptance_of(s['status'])}]" for s in rows}
    sid = st.selectbox("건 선택", [s["id"] for s in rows], format_func=lambda i: label[i],
                       key="adm_sel")
    sub = store.get_submission(DB_PATH, sid)
    with st.form("adm_billing"):
        a, b, c = st.columns(3)
        quote = a.number_input("견적액(만원)", value=float(sub["quote_amount"] or 0.0), step=10.0)
        qsent = b.text_input("견적발송일", value=sub["quote_sent"] or "", placeholder="예: 2026-01-01")
        csent = c.text_input("계약서발송일", value=sub["contract_sent"] or "", placeholder="예: 2026-03-01")
        d, e = st.columns(2)
        coll = d.selectbox("수금상황", store.COLLECTION_STATUSES,
                           index=_idx(store.COLLECTION_STATUSES, sub["collection_status"]))
        try:
            _pd = dt.date.fromisoformat(sub["promised_date"]) if sub["promised_date"] else None
        except Exception:
            _pd = None
        promised = e.date_input("약속일", value=_pd)
        memo = st.text_input("비고", value=sub["note"] or "")
        if st.form_submit_button("💾 영업정보 저장", type="primary"):
            store.set_submission_meta(DB_PATH, sid, quote_amount=quote, note=memo,
                                      quote_sent=qsent, contract_sent=csent, collection_status=coll,
                                      promised_date=(promised.isoformat() if promised else ""))
            st.success("저장했습니다."); st.rerun()


def _admin_qa_status():
    st.subheader("2. 질의응답현황")
    st.caption("ㅁ 답변은 담당자 메일로도 자동 전송")
    qrows = store.qa_question_rows(DB_PATH)
    if not qrows:
        st.info("등록된 질의가 없습니다.")
        return
    st.dataframe(pd.DataFrame([{
        "번호": f"Q{r['qno']}" if r.get("qno") else "-", "날짜": r["date"],
        "제목": r.get("title") or "-", "문의내용": r["body"], "기업명": r["company_name"],
        "답변여부": r["answered"], "답변자": r["answered_by"],
    } for r in qrows]), width="stretch", hide_index=True)
    waiting = [r for r in qrows if r["answered"] == "-"]
    if waiting:
        st.warning(f"미답변 {len(waiting)}건 — 계리사 화면에서 답변하세요.")


def _admin_revenue():
    st.subheader("3. 매출현황")
    subs = [s for s in store.list_submissions(DB_PATH)
            if s["quote_amount"] is not None and s["status"] != "cancelled"]
    if not subs:
        st.info("견적액이 입력된 건이 없습니다. 기업조회에서 영업정보를 입력하세요.")
        return
    total = sum(float(s["quote_amount"] or 0) for s in subs)
    collected = sum(float(s["quote_amount"] or 0) for s in subs
                    if (s["collection_status"] or "") == "수금완료")
    m1, m2, m3 = st.columns(3)
    m1.metric("총 견적액", f"{total:,.0f} 만원")
    m2.metric("수금완료", f"{collected:,.0f} 만원")
    m3.metric("미수금", f"{total - collected:,.0f} 만원")
    st.divider()
    st.dataframe(pd.DataFrame([{
        "기업명": s["company_name"], "산출기준일": s["valuation_date"],
        "견적액(만원)": s["quote_amount"], "견적발송": s["quote_sent"] or "미발송",
        "계약서발송": s["contract_sent"] or "미발송",
        "수금상황": s["collection_status"] or "미청구",
    } for s in subs]), width="stretch", hide_index=True)
    by_status = {}
    for s in subs:
        k = s["collection_status"] or "미청구"
        by_status[k] = by_status.get(k, 0) + float(s["quote_amount"] or 0)
    st.markdown("##### 수금상황별 합계")
    st.bar_chart(pd.Series(by_status, name="만원"))


def _admin_new_company(user):
    st.subheader("기업 신규등록 (기업 + 담당자 계정)")
    with st.form("new_company"):
        cn = st.text_input("회사명")
        a, b = st.columns(2)
        un = a.text_input("담당자 로그인 아이디")
        pwd = b.text_input("초기 비밀번호", type="password")
        dn = st.text_input("담당자 표시 이름", placeholder="예: 가나전자 인사담당")
        if st.form_submit_button("등록", type="primary"):
            if not (cn and un and pwd):
                st.error("회사명·아이디·비밀번호는 필수입니다.")
            elif auth.user_exists(DB_PATH, un):
                st.error("이미 존재하는 아이디입니다.")
            else:
                try:
                    cid_new = auth.create_company(DB_PATH, cn, now())
                    auth.create_user(DB_PATH, un, pwd, "client", now(),
                                     company_id=cid_new, display_name=dn or un)
                    store.save_sales(DB_PATH, cid_new,
                                     {"contract_status": "신규접수",
                                      "received_date": dt.date.today().isoformat()},
                                     user["id"], now())
                    st.success(f"'{cn}' 등록 완료 (담당자 계정: {un}).")
                except Exception as e:  # noqa: BLE001
                    st.error(f"등록 실패: {e}")

    st.divider()
    st.markdown("##### 등록 기업 목록")
    sales = store.list_sales(DB_PATH)
    if sales:
        st.dataframe(pd.DataFrame([{
            "회사": s["company_name"], "담당자": s.get("contact_name") or "-",
            "연락처": s.get("contact_phone") or "-", "계약상태": s.get("contract_status") or "-",
            "접수일": s.get("received_date") or "-",
        } for s in sales]), width="stretch", hide_index=True)


def _admin_company_info(user):
    st.subheader("기업정보 조회 / 수정")
    sales = store.list_sales(DB_PATH)
    names = {s["company_id"]: s["company_name"] for s in sales}
    if not names:
        st.info("등록된 기업이 없습니다.")
        return
    sel = st.selectbox("기업 선택", list(names), format_func=lambda i: names[i], key="adm_ci")
    sv = store.get_sales(DB_PATH, sel) or {}
    with st.form("sales_form"):
        a, b = st.columns(2)
        cname = a.text_input("담당자 이름", sv.get("contact_name") or "")
        ctitle = b.text_input("직급", sv.get("contact_title") or "")
        cphone = a.text_input("전화(사무실)", sv.get("contact_phone") or "")
        cmobile = b.text_input("전화(모바일)", sv.get("contact_mobile") or "")
        cemail = a.text_input("이메일", sv.get("contact_email") or "")
        cmonth = b.text_input("결산월", sv.get("settlement_month") or "", placeholder="예: 12월")
        caddr = st.text_input("주소", sv.get("address") or "")
        cs = a.selectbox("계약 진행상태", store.CONTRACT_STATUSES,
                         index=_idx(store.CONTRACT_STATUSES, sv.get("contract_status")))
        ap = b.selectbox("결재 진행상황", store.APPROVAL_STATUSES,
                         index=_idx(store.APPROVAL_STATUSES, sv.get("approval_status")))
        sr = st.text_area("특별 요구사항", sv.get("special_requests") or "")
        if st.form_submit_button("💾 기업정보 저장", type="primary"):
            store.save_sales(DB_PATH, sel, {
                "contact_name": cname, "contact_title": ctitle, "contact_phone": cphone,
                "contact_mobile": cmobile, "contact_email": cemail, "settlement_month": cmonth,
                "address": caddr, "contract_status": cs, "approval_status": ap,
                "special_requests": sr,
                "received_date": sv.get("received_date") or dt.date.today().isoformat(),
            }, user["id"], now())
            st.success("기업정보를 저장했습니다.")

    st.markdown("##### 상담 이력 (통화·미팅 등)")
    with st.form("interaction_form", clear_on_submit=True):
        a, b, c = st.columns([2, 2, 2])
        idate = a.date_input("일시", dt.date.today())
        itype = b.selectbox("유형", store.INTERACTION_TYPES)
        istaff = c.text_input("담당 직원")
        isummary = st.text_input("요약")
        if st.form_submit_button("＋ 이력 추가"):
            if isummary:
                store.add_interaction(DB_PATH, sel, idate.isoformat(), itype,
                                      isummary, istaff, user["id"], now())
                st.success("상담 이력을 추가했습니다.")
            else:
                st.error("요약을 입력하세요.")
    inter = store.list_interactions(DB_PATH, sel)
    if inter:
        st.dataframe(pd.DataFrame([{
            "일시": i["ts"], "유형": i["itype"], "요약": i["summary"], "담당": i["staff"] or "-",
        } for i in inter]), width="stretch", hide_index=True)
    else:
        st.caption("상담 이력이 없습니다.")


# ---------------------------------------------------------------------------
# 라우팅
# ---------------------------------------------------------------------------

if user["role"] == "client":
    page_client(user)
elif user["role"] == "actuary":
    page_actuary(user)
elif user["role"] == "admin":
    page_admin(user)
else:
    st.error("알 수 없는 역할입니다.")
