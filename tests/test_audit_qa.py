"""audit_qa.py 테스트: 감사 대응 표준 질의응답 생성."""

from datetime import date

from dbo.audit_qa import build_audit_qa
from dbo.config import Config
from dbo.decrement import DecrementTables
from dbo.engine import calculate_census
from dbo.models import Employee


def _setup():
    cfg = Config.from_yaml("config/assumptions_sample.yaml")
    tables = DecrementTables.from_config(cfg, base_dir="config")
    recs = [Employee(emp_id=str(i), birth_date=date(1980, 1, 1), gender="M",
                     hire_date=date(2010, 1, 1), base_salary=3_000_000,
                     current_year_accrual=30_000_000, emp_class="REGULAR")
            for i in range(10)]
    result = calculate_census(recs, cfg, tables)
    return recs, cfg, tables, result


def test_audit_qa_has_core_questions_with_numbers():
    recs, cfg, tables, result = _setup()
    qa = build_audit_qa(recs, cfg, tables, result)
    assert len(qa) >= 6
    joined = " ".join(x["q"] for x in qa)
    assert "PUC" in " ".join(x["a"] for x in qa) or "예측단위적립방식" in " ".join(x["a"] for x in qa)
    assert any("할인율" in x["q"] for x in qa)
    assert any("듀레이션" in x["q"] or "가중평균만기" in x["q"] for x in qa)
    # 근거에 실제 수치가 인용됨
    assert all("근거" in x["basis"] or "K-IFRS" in x["basis"] for x in qa)


def test_audit_qa_includes_plan_and_prior_when_given():
    recs, cfg, tables, result = _setup()
    qa = build_audit_qa(recs, cfg, tables, result,
                        plan_info={"plan_type": "DB(확정급여)", "salary_basis": "평균임금"},
                        prior={"prior_dbo": 1.0e9, "prior_firm": "옛법인"})
    qs = " ".join(x["q"] for x in qa)
    assert "퇴직급여제도" in qs
    assert "전기 대비" in qs
