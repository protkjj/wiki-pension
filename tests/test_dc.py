"""DC 부담금 산정·명부 파싱·납부명세서 테스트."""

from dbo.dc import (build_dc_payment_xlsx, build_dc_template,
                    compute_dc_contributions, dc_return_stats, monthly_split,
                    parse_dc_roster)


def test_dc_template_roundtrip():
    roster = parse_dc_roster(build_dc_template())
    assert len(roster) == 3
    r0 = roster[0]
    assert r0["emp_id"] == "A001" and r0["wage"] == 42_000_000
    # 계좌번호는 문자열로 보존(앞자리·하이픈 유지)
    assert r0["account"] == "123-456-7890"


def test_compute_dc_contributions_prorate():
    roster = parse_dc_roster(build_dc_template())
    rows, summ = compute_dc_contributions(roster, 2025, 12)
    by = {r["emp_id"]: r for r in rows}
    # 계속 재직자: 연간임금 × 1/12
    assert by["A001"]["contribution"] == 3_500_000       # 42,000,000 / 12
    assert by["A002"]["contribution"] == 3_000_000       # 36,000,000 / 12
    # 2025-04-01 입사자: 재직일수(275) 일할
    assert by["A003"]["days"] == 275
    assert by["A003"]["contribution"] == round(30_000_000 / 12 * 275 / 365)
    assert summ["n"] == 3 and summ["n_missing_account"] == 0
    assert summ["total"] == sum(r["contribution"] for r in rows)


def test_dc_missing_account_and_wage_flagged():
    roster = [
        {"emp_id": "X1", "dept": "", "wage": None, "bank": "", "account": "",
         "hire": None, "leave": None},
    ]
    rows, summ = compute_dc_contributions(roster, 2025, 12)
    assert summ["n_missing_account"] == 1 and summ["n_missing_wage"] == 1
    assert rows[0]["contribution"] == 0


def test_dc_payment_xlsx_builds():
    roster = parse_dc_roster(build_dc_template())
    rows, summ = compute_dc_contributions(roster, 2025, 12)
    xlsx = build_dc_payment_xlsx(rows, summ, "테스트사", 2025, biz_no="1234567", due_date="2025-01-31")
    assert xlsx[:2] == b"PK" and len(xlsx) > 2000   # 유효한 xlsx(zip) 헤더


def test_dc_monthly_and_adjust_floor_cap():
    roster = parse_dc_roster(build_dc_template())
    # 월할 + 가감률 1.1
    rows, summ = compute_dc_contributions(roster, 2025, 12, proration="monthly", adjust=1.1)
    by = {r["emp_id"]: r for r in rows}
    assert by["A003"]["months"] == 9 and by["A003"]["frac"] == 0.75      # 4~12월
    assert by["A001"]["contribution"] == round(42_000_000 / 12 * 1.1)     # 가감 1.1, 전년재직
    # 상·하한(연간 부담금 기준)
    rows2, _ = compute_dc_contributions(roster, 2025, 12, cap=3_200_000)
    assert {r["emp_id"]: r for r in rows2}["A001"]["contribution"] == 3_200_000
    rows3, _ = compute_dc_contributions(roster, 2025, 12, floor=3_400_000)
    assert {r["emp_id"]: r for r in rows3}["A002"]["contribution"] == 3_400_000


def test_dc_monthly_split_sums():
    roster = parse_dc_roster(build_dc_template())
    rows, _ = compute_dc_contributions(roster, 2025, 12)
    for r in monthly_split(rows, 12):
        assert sum(r["monthly"]) == r["contribution"]   # 분할 합 = 연간 부담금


def test_dc_return_stats():
    roster = parse_dc_roster(build_dc_template())        # 양식 예시에 적립원금·평가액 포함
    rows, _ = compute_dc_contributions(roster, 2025, 12)
    have, avg, tp, tv = dc_return_stats(rows)
    assert len(have) == 3 and tp > 0 and tv > tp        # 평가액 > 원금 → 양의 수익률
    assert abs(avg - (tv / tp - 1)) < 1e-9
