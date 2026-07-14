"""보조 입력 양식 3종 — 사외적립자산 · 기타장기 · 명부확인용요약표.

기업 담당자가 하나하나 화면에 입력하는 번거로움을 줄이기 위해,
각 항목을 ① 양식(xlsx) 다운로드 → ② 작성 후 업로드 → ③ 결과값만 화면에서
편집 미리보기 하는 흐름으로 처리한다.

- build_*_template(): openpyxl로 양식(녹색 입력칸)을 생성한다.
- parse_*_upload(): 업로드 파일에서 입력값(라벨 기준 스캔)을 읽어 dict/rows로 반환.
- compute_census_summary(): 재직자명부 레코드로 요약표 '자동산출' 값을 계산한다.

명부확인용요약표는 재직자명부 등록 이후에만 의미가 있으며, 양식 다운로드 시
'등록부명에서 자동산출된 값'(G열)을 재직자명부에서 산출해 채워 준다.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Dict, List, Optional, Union

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

# 사용자 제공 양식 원본(시인성 좋은 실제 양식) — 3개 시트를 그대로 다운로드에 사용한다.
ASSET_PATH = Path(__file__).resolve().parent / "assets" / "aux_templates.xlsx"
_SHEET_FUNDING = "사외적립자산"
_SHEET_OTHER_LT = "기타 장기"
_SHEET_SUMMARY = "명부확인용요약표"

# 명부확인용요약표 G열(자동산출) 셀 ↔ compute_census_summary 키 매핑(사용자 양식 좌표).
_SUMMARY_G_CELLS = {
    "G6": "재직_임원", "G7": "재직_직원", "G8": "재직_계약직", "G9": "재직_합계",
    "G14": "중간정산자수",
    "G16": "추계액_임원", "G17": "추계액_직원", "G18": "추계액_계약직", "G19": "추계액_합계",
    "G22": "중간정산금액",
}


def _extract_sheet(sheet_name: str):
    """원본 양식 워크북에서 지정 시트만 남긴 워크북을 반환(스타일·서식 보존)."""
    wb = load_workbook(ASSET_PATH)
    for s in list(wb.sheetnames):
        if s != sheet_name:
            del wb[s]
    return wb


def _to_bytes(wb) -> bytes:
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

# 녹색(입력) 칸 · 헤더 스타일 -------------------------------------------------
_GREEN = PatternFill("solid", fgColor="E2EFDA")     # 입력칸(녹색)
_HEAD = PatternFill("solid", fgColor="DCE6F1")       # 헤더(연파랑)
_AUTO = PatternFill("solid", fgColor="FFF2CC")       # 자동산출(연노랑)
_BOLD = Font(name="맑은 고딕", size=10, bold=True)
_NORM = Font(name="맑은 고딕", size=10)
_TITLE = Font(name="맑은 고딕", size=13, bold=True, color="1F4E79")
_CEN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
_thin = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _norm(s) -> str:
    """라벨 정규화 — 공백·괄호·기호 제거로 스캔 매칭 안정화."""
    if s is None:
        return ""
    t = str(s)
    for ch in " \n\t()（）[]{}·.":
        t = t.replace(ch, "")
    return t.strip()


def _open_first_sheet(source: Union[str, bytes]):
    if isinstance(source, (bytes, bytearray)):
        wb = load_workbook(io.BytesIO(source), data_only=True)
    else:
        wb = load_workbook(str(source), data_only=True)
    name = next((s for s in wb.sheetnames if "요령" not in s), wb.sheetnames[0])
    return wb[name]


# ===========================================================================
# 1) 사외적립자산 현황
# ===========================================================================
# (라벨 → funding_status data_json 키). 라벨은 _norm 적용 기준.
_FUNDING_ROWS = [
    ("기초잔액", "기초 잔액 (A)", "기초부터 이월된 기초 잔액"),
    ("입금액", "입금(수탁)액 (B)", "기초부터 누계 입금(수탁)액"),
    ("지급_퇴직", "급여지급-퇴직 (a)", "기초부터 누계 급여지급(인출)-퇴직"),
    ("지급_중간정산", "급여지급-중간정산 (b)", ""),
    ("지급_DC전환", "급여지급-DC전환 (c)", ""),
    ("관계사전입", "관계사전입 (d)", "기초부터 누계 관계사전입"),
    ("관계사전출", "관계사전출 (e)", "기초부터 누계 관계사전출"),
    ("사업결합", "사업결합 (f)", "기초부터 누계 사업결합액"),
    ("사업처분", "사업처분 (g)", "기초부터 누계 사업처분액"),
    ("투자수익", "투자수익 (h)", "기초부터 누계 투자수익"),
    ("운용수수료", "운용관리수수료 (i)", "기초부터 누계 운용관리수수료"),
    ("기말_퇴직연금", "기말잔액-퇴직연금", "작성기준일 현재 공정가치"),
    ("기말_퇴직신탁", "기말잔액-퇴직신탁", ""),
    ("기말_퇴직보험", "기말잔액-퇴직보험", ""),
    ("국민연금전환금", "국민연금전환금", "국민연금전환금이 있는 경우 입력"),
]
# 파싱 라벨 별칭(사용자가 양식을 바꿔도 잡히도록)
_FUNDING_ALIASES = {
    "기초잔액": ["기초잔액", "기초잔액A"],
    "입금액": ["입금수탁액B", "입금액", "입금수탁액"],
    "지급_퇴직": ["급여지급퇴직a", "퇴직a", "급여지급퇴직"],
    "지급_중간정산": ["급여지급중간정산b", "중간정산b", "급여지급중간정산"],
    "지급_DC전환": ["급여지급DC전환c", "DC전환c", "급여지급DC전환"],
    "관계사전입": ["관계사전입d", "관계사전입"],
    "관계사전출": ["관계사전출e", "관계사전출"],
    "사업결합": ["사업결합f", "사업결합"],
    "사업처분": ["사업처분g", "사업처분"],
    "투자수익": ["투자수익h", "투자수익"],
    "운용수수료": ["운용관리수수료i", "운용관리수수료", "운용수수료"],
    "기말_퇴직연금": ["기말잔액퇴직연금", "퇴직연금"],
    "기말_퇴직신탁": ["기말잔액퇴직신탁", "퇴직신탁"],
    "기말_퇴직보험": ["기말잔액퇴직보험", "퇴직보험"],
    "국민연금전환금": ["국민연금전환금"],
}


def build_funding_template(valuation_date: str = "") -> bytes:
    """사외적립자산 현황 양식(xlsx) — 사용자 제공 원본 시트 그대로.

    작성기준일 칸은 병합 셀이라 원본 그대로 두고, 사용자가 직접 입력한다.
    """
    return _to_bytes(_extract_sheet(_SHEET_FUNDING))


def parse_funding_upload(source: Union[str, bytes]) -> Dict:
    """사외적립자산 양식 업로드 → funding_status data_json dict.

    라벨(내용 열)을 정규화해 값(금액 열)을 읽는다. 작성기준일도 함께 반환.
    반환: {"_valuation_date": str|None, "공시방법": str, ...금액키: float}
    """
    ws = _open_first_sheet(source)
    grid = list(ws.iter_rows(values_only=True))
    alias_norm = {k: [_norm(a) for a in al] for k, al in _FUNDING_ALIASES.items()}

    out: Dict = {k: 0.0 for k, _l, _n in _FUNDING_ROWS}
    out["공시방법"] = ""
    out["_valuation_date"] = None

    for row in grid:
        if not row:
            continue
        cells = list(row)
        # 각 셀 라벨을 스캔, 같은 행에서 첫 숫자값을 값으로 사용
        labels = [_norm(c) for c in cells]
        # 값 후보: 이 행에서 숫자로 해석되는 셀
        numvals = [c for c in cells if isinstance(c, (int, float))]
        first_num = float(numvals[0]) if numvals else None

        for key, al in alias_norm.items():
            if any(a and a in lb for lb in labels for a in al):
                if first_num is not None:
                    out[key] = first_num
        # 작성기준일
        if any("작성기준일" in lb or "산출기준일" in lb for lb in labels):
            for c in cells:
                if isinstance(c, str) and len(c) >= 8 and any(ch.isdigit() for ch in c):
                    out["_valuation_date"] = c.strip()
        # 공시방법
        if any("공시방법" in lb for lb in labels):
            for c in cells:
                if isinstance(c, str) and ("공시" in c or c.strip() in ("①", "②", "③")):
                    out["공시방법"] = c.strip()
    return out


# ===========================================================================
# 2) 기타장기 포상제도
# ===========================================================================
def build_other_lt_template() -> bytes:
    """기타장기 포상제도 양식(xlsx) — 사용자 제공 원본 시트 그대로."""
    return _to_bytes(_extract_sheet(_SHEET_OTHER_LT))


def parse_other_lt_upload(source: Union[str, bytes]) -> Dict:
    """기타장기 양식 업로드 → {'지급내역': [...], '포상제도': [...]}.

    라벨행(헤더)을 찾아 그 아래 데이터행을 읽는다. 값이 있는 행만 반환.
    """
    ws = _open_first_sheet(source)
    grid = [list(r) for r in ws.iter_rows(values_only=True)]

    def _find_header(keys) -> Optional[int]:
        for ridx, row in enumerate(grid):
            norm = [_norm(c) for c in row]
            if all(any(k in nb for nb in norm) for k in keys):
                return ridx
        return None

    def _rows_after(hidx, ncols, stopwords=("합계", "ⓑ", "ⓒ", "포상제도")):
        out = []
        if hidx is None:
            return out
        for row in grid[hidx + 1:]:
            first = _norm(row[1]) if len(row) > 1 else ""
            if any(sw and sw in first for sw in map(_norm, stopwords)):
                break
            vals = [row[1 + k] if len(row) > 1 + k else None for k in range(ncols)]
            if any(v not in (None, "") for v in vals):
                out.append(vals)
        return out

    a_idx = _find_header(["근속년수", "근속자", "축하금"])
    b_idx = _find_header(["근속년수", "지급액", "유급휴가"])
    give = _rows_after(a_idx, 4)
    award = _rows_after(b_idx, 7)
    return {"지급내역": give, "포상제도": award}


# ===========================================================================
# 3) 명부확인용요약표
# ===========================================================================
# G열(자동산출) 항목 — (요약키, 라벨행 표시). 재직자명부에서 산출 가능한 항목만.
CENSUS_SUMMARY_AUTO_KEYS = [
    "재직_임원", "재직_직원", "재직_계약직", "재직_합계",
    "추계액_임원", "추계액_직원", "추계액_계약직", "추계액_합계",
    "중간정산자수", "중간정산금액",
]


def compute_census_summary(records) -> Dict[str, float]:
    """재직자명부 레코드 → 요약표 '자동산출' 값(G열용).

    재직자수(임원/직원/계약직/합계), 당년도 퇴직금추계액(구분별/합계),
    중간정산자수·중간정산금액 합계. (퇴직자·DC전환·관계사·사업결합은 다른 명부 필요 → 제외)
    """
    def _cls(r):
        v = getattr(r, "emp_class", None)
        return getattr(v, "value", v)

    n_exec = n_reg = n_con = 0
    a_exec = a_reg = a_con = 0.0
    interim_n = 0
    interim_amt = 0.0
    for r in records:
        cls = _cls(r)
        accr = float(getattr(r, "current_year_accrual", 0) or 0)
        if cls == "EXECUTIVE":
            n_exec += 1; a_exec += accr
        elif cls == "CONTRACT":
            n_con += 1; a_con += accr
        else:
            n_reg += 1; a_reg += accr
        if getattr(r, "interim_settlement_date", None) is not None:
            interim_n += 1
            interim_amt += float(getattr(r, "interim_settlement_amount", 0) or 0)
    return {
        "재직_임원": n_exec, "재직_직원": n_reg, "재직_계약직": n_con,
        "재직_합계": n_exec + n_reg + n_con,
        "추계액_임원": a_exec, "추계액_직원": a_reg, "추계액_계약직": a_con,
        "추계액_합계": a_exec + a_reg + a_con,
        "중간정산자수": interim_n, "중간정산금액": interim_amt,
    }


# 요약표 항목 행 정의 — (항목, 세부, 자동산출키|None, 비고)
_SUMMARY_ROWS = [
    ("평가대상", "재직자수-임원", "재직_임원", '"재직자 명부"의 임원 수'),
    ("", "재직자수-직원", "재직_직원", '"재직자 명부"의 직원 수'),
    ("", "재직자수-계약직", "재직_계약직", '"재직자 명부"의 계약직 수'),
    ("", "재직자수-합계", "재직_합계", "재직자 명부 합계와 일치"),
    ("", "퇴직자수-임원", None, '"퇴직자 명부" 필요 — 회사 입력'),
    ("", "퇴직자수-직원", None, ""),
    ("", "퇴직자수-계약직", None, ""),
    ("", "퇴직자수-합계", None, ""),
    ("", "중간정산자수", "중간정산자수", "당기 중간정산자 수"),
    ("", "DC전환자수", None, '"DC전환자 명부" 필요 — 회사 입력'),
    ("퇴직금추계액(원)", "임원", "추계액_임원", "당년도 퇴직금추계액-임원"),
    ("", "직원", "추계액_직원", "당년도 퇴직금추계액-직원"),
    ("", "계약직", "추계액_계약직", "당년도 퇴직금추계액-계약직"),
    ("", "합계", "추계액_합계", "재직자 명부 추계액 합계와 일치"),
    ("퇴직부채 감소(증가)액(원)", "급여지급-퇴직금 (a)", None, '"퇴직자 명부" 필요 — 회사 입력'),
    ("", "급여지급-중간정산 (b)", "중간정산금액", "중간정산금액 합계"),
    ("", "급여지급-DC전환 (c)", None, '"DC전환자 명부" 필요 — 회사 입력'),
    ("", "관계사전입 (d)", None, '"추가 명부" 필요 — 회사 입력'),
    ("", "관계사전출 (e)", None, ""),
    ("", "사업결합 (f)", None, ""),
    ("", "사업처분 (g)", None, ""),
]


def build_census_summary_template(summary: Optional[Dict[str, float]] = None,
                                  valuation_date: str = "") -> bytes:
    """명부확인용요약표 양식(xlsx) — 사용자 원본 시트에 G열(자동산출)을 채워 제공.

    summary: compute_census_summary() 결과. G열의 '등록부명에서 자동산출된 값' 칸에
    재직자명부 산출값(재직자수·추계액·중간정산)을 기입한다.
    """
    s = summary or {}
    wb = _extract_sheet(_SHEET_SUMMARY)
    ws = wb.worksheets[0]
    for cell, key in _SUMMARY_G_CELLS.items():
        val = s.get(key)
        if val is not None:
            ws[cell] = val
    if valuation_date:
        ws["F5"] = f"{valuation_date} 기준 요약표"   # 작성기준일 표시
    return _to_bytes(wb)


def parse_census_summary_upload(source: Union[str, bytes]) -> Dict:
    """명부확인용요약표 업로드 → 행별 {항목, 세부, 입력(F), 자동(G)} + 작성기준일."""
    ws = _open_first_sheet(source)
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    out: Dict = {"_valuation_date": None, "rows": []}
    cur_item = ""
    for row in grid:
        if not row:
            continue
        b = row[1] if len(row) > 1 else None       # B열 항목
        c = row[2] if len(row) > 2 else None       # C열 세부(재직자수 등)
        d = row[3] if len(row) > 3 else None       # D열 상세(임원/직원 등)
        f_val = row[5] if len(row) > 5 else None    # F열 회사 입력
        g_val = row[6] if len(row) > 6 else None    # G열 자동산출
        if b is not None and str(b).strip():
            cur_item = str(b).strip()
        # 작성기준일 행
        if c is not None and _norm(c) == "작성기준일":
            if f_val:
                out["_valuation_date"] = str(f_val).strip()
            continue
        label = " · ".join([str(x).strip() for x in (c, d) if x is not None and str(x).strip()])
        if not label:
            continue
        if _norm(label) in ("세부", "항목", "내용", "등록부명에서자동산출된값"):
            continue
        if f_val is None and g_val is None:
            continue
        out["rows"].append({"항목": cur_item, "세부": label,
                            "입력(회사)": f_val, "자동산출(명부)": g_val})
    return out
