"""퇴직자명부 기반 경험 퇴직률 산출 테스트."""

import datetime as dt
import io

import openpyxl

from dbo import experience as EXP


def _retiree_xlsx(rows):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["사원번호", "생년월일", "성별", "입사일자", "퇴직일_DC전환일",
               "퇴직금_DC전환금", "종업원구분", "사유"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_parse_retiree_census():
    b = _retiree_xlsx([
        ["R1", "19850101", 1, "20100101", "20230601", 30000000, 1, 1],
        ["R2", "19900101", 2, "20150101", "20230101", 20000000, 1, 2],
    ])
    recs = EXP.parse_retiree_census(b)
    assert len(recs) == 2
    assert recs[0]["emp_id"] == "R1"
    assert recs[0]["birth"] == dt.date(1985, 1, 1)
    assert recs[0]["leave"] == dt.date(2023, 6, 1)
    assert recs[1]["reason"] == "2"


def test_compute_withdrawal_rates():
    recs = [
        {"birth": dt.date(1990, 1, 1), "leave": dt.date(2023, 1, 1)},   # age 33 → band30
        {"birth": dt.date(1985, 1, 1), "leave": dt.date(2023, 6, 1)},   # age 38 → band35
    ]
    active_ages = [32] * 10 + [37] * 20      # band30: 10, band35: 20
    res = EXP.compute_withdrawal_rates(recs, active_ages, obs_years=3, retirement_age=60)
    bands = {b["밴드"]: b for b in res["bands"]}
    assert bands["30-34"]["퇴직건수"] == 1
    assert bands["30-34"]["경험퇴직률"] == round(1 / (10 * 3), 5)
    assert bands["35-39"]["경험퇴직률"] == round(1 / (20 * 3), 5)
    assert res["summary"]["n_withdrawals"] == 2
    # rows: 밴드값이 연령으로 펼쳐짐
    r33 = next(r for r in res["rows"] if r["age"] == 33)
    assert r33["withdrawal"] == round(1 / 30, 6)


def test_compute_withdrawal_skips_missing():
    recs = [{"birth": None, "leave": dt.date(2023, 1, 1)}]     # 생년월일 없음 → skip
    res = EXP.compute_withdrawal_rates(recs, [30] * 5, obs_years=3)
    assert res["summary"]["skipped"] == 1
    assert res["summary"]["n_withdrawals"] == 0


def test_withdrawal_boundary_age_at_retirement():
    """정년(hi)에 정확히 걸리는 연령(퇴직자·재직자 모두)이 있어도 KeyError 없이 산출.

    회귀: bands=range(15,60,5)의 최대 밴드는 55인데 _band_of(60)이 밴드 60을 반환해
    active_cnt[60]에서 KeyError가 나던 문제.
    """
    # 퇴직 시 연령 60(1964년생, 2024년 퇴직) 포함
    b = _retiree_xlsx([
        ["R1", "19640101", 1, "19900101", "20240101", 50000000, 1, 1],
        ["R2", "19850505", 2, "20100101", "20240301", 12000000, 1, 1],
    ])
    recs = EXP.parse_retiree_census(b)
    # 재직자 연령에 정년(60) 포함
    res = EXP.compute_withdrawal_rates(recs, [45, 60, 38, 60], 3.0, 60)
    assert res["summary"]["n_withdrawals"] == 2
    # 정년 연령은 최상위 밴드(55-59)로 접혀 들어간다
    top = [row for row in res["bands"] if row["밴드"].startswith("55")][0]
    assert top["재직자수"] == 2 and top["퇴직건수"] == 1


def _curve_xlsx(pairs, header=("연령", "퇴직률")):
    import openpyxl, io
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(list(header))
    for a, r in pairs:
        ws.append([a, r])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_parse_withdrawal_curve_fraction_and_percent():
    # 소수 입력
    b = _curve_xlsx([(20, 0.15), (30, 0.08), (40, 0.05), (60, 0.02)])
    rows = EXP.parse_withdrawal_curve(b)
    assert len(rows) == 4
    assert rows[0] == {"age": 20, "withdrawal": 0.15, "raise_rate": None,
                       "mort_m": None, "mort_f": None}
    # % 입력(>1)은 100으로 나눔
    b2 = _curve_xlsx([(20, 15), (30, 8)])
    rows2 = EXP.parse_withdrawal_curve(b2)
    assert rows2[0]["withdrawal"] == 0.15 and rows2[1]["withdrawal"] == 0.08


def test_parse_withdrawal_curve_skips_headers_and_sorts():
    # 순서 뒤섞이고 빈/문자 행 섞여도 정수연령+숫자율만 채택, 정렬
    b = _curve_xlsx([(40, 0.05), ("합계", None), (20, 0.15), (None, None)])
    rows = EXP.parse_withdrawal_curve(b)
    assert [r["age"] for r in rows] == [20, 40]
