"""models.py 테스트: 스키마 검증, 기본값, Enum 처리, 근속 기산."""

from datetime import date

import pytest
from pydantic import ValidationError

from dbo.models import EmpClass, Employee, Gender, PlanType


def _valid_kwargs(**over):
    base = dict(
        emp_id="10001",
        birth_date=date(1985, 5, 1),
        gender="M",
        hire_date=date(2010, 3, 1),
        base_salary=3_000_000,
        current_year_accrual=50_000_000,
        emp_class="REGULAR",
    )
    base.update(over)
    return base


def test_minimal_valid_record_uses_defaults():
    emp = Employee(**_valid_kwargs())
    assert emp.plan_type == PlanType.DB_NORMAL     # 기본 제도구분 1
    assert emp.multiplier == 1.0                   # 기본 적용배수
    assert emp.interim_settlement_date is None
    assert emp.interim_settlement_amount is None


def test_enum_coercion_from_strings_and_ints():
    emp = Employee(**_valid_kwargs(gender="F", emp_class="EXECUTIVE", plan_type=2))
    assert emp.gender == Gender.F
    assert emp.emp_class == EmpClass.EXECUTIVE
    assert emp.plan_type == PlanType.SIMPLIFIED


def test_emp_id_is_stripped_and_stringified():
    emp = Employee(**_valid_kwargs(emp_id="  A-42  "))
    assert emp.emp_id == "A-42"


def test_invalid_gender_rejected():
    with pytest.raises(ValidationError):
        Employee(**_valid_kwargs(gender="X"))


def test_invalid_plan_type_rejected():
    with pytest.raises(ValidationError):
        Employee(**_valid_kwargs(plan_type=9))


def test_extra_field_forbidden():
    # 스키마 밖 필드(예: 성명)는 거부되어야 한다 (개인정보 보호).
    with pytest.raises(ValidationError):
        Employee(**_valid_kwargs(name="홍길동"))


def test_missing_required_field_rejected():
    kw = _valid_kwargs()
    del kw["base_salary"]
    with pytest.raises(ValidationError):
        Employee(**kw)


def test_service_start_date_regular():
    emp = Employee(**_valid_kwargs())
    assert emp.service_start_date() == emp.hire_date


def test_service_start_date_with_interim_settlement():
    # 중간정산자는 중간정산기준일부터 근속을 다시 기산한다.
    emp = Employee(
        **_valid_kwargs(
            interim_settlement_date=date(2018, 1, 1),
            interim_settlement_amount=20_000_000,
        )
    )
    assert emp.service_start_date() == date(2018, 1, 1)
