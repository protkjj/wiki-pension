"""config.py 테스트: YAML 로드, 기본값, 스칼라 축약 표기, 정년/할인율 접근자."""

from datetime import date

from dbo.config import Config
from dbo.models import EmpClass

SAMPLE = "config/assumptions_sample.yaml"


def test_load_sample_yaml():
    cfg = Config.from_yaml(SAMPLE)
    assert cfg.valuation_date == date(2025, 12, 31)
    assert cfg.discount_rate.flat == 0.045
    assert cfg.salary_increase_rate.flat == 0.03
    assert cfg.rounding == 1


def test_scalar_shorthand_normalization():
    # discount_rate / salary_increase_rate / retirement_age 스칼라 축약 표기 지원.
    cfg = Config.from_dict(
        {
            "valuation_date": "2025-12-31",
            "discount_rate": 0.05,
            "salary_increase_rate": 0.025,
            "retirement_age": 62,
        }
    )
    assert cfg.discount_rate.flat == 0.05
    assert cfg.salary_increase_rate.flat == 0.025
    assert cfg.retirement_age.default == 62


def test_defaults_applied_when_omitted():
    cfg = Config.from_dict(
        {
            "valuation_date": "2025-12-31",
            "discount_rate": 0.05,
            "salary_increase_rate": 0.03,
        }
    )
    assert cfg.decrement_timing == "end_of_year"
    assert cfg.salary_increase_timing == "start_of_year"
    assert cfg.service_day_count == "act/365"
    assert cfg.retirement_rate_basis == "age"
    assert cfg.csc_method == "one_year_slice"
    assert cfg.retirement_age.default == 60


def test_retirement_age_by_class():
    cfg = Config.from_yaml(SAMPLE)
    assert cfg.retirement_age.for_class(EmpClass.EXECUTIVE) == 65
    assert cfg.retirement_age.for_class(EmpClass.REGULAR) == 60
    # by_class 미지정 구분은 default로 fallback (샘플엔 CONTRACT=60 명시되어 있음)
    assert cfg.retirement_age.for_class(EmpClass.CONTRACT) == 60


def test_discount_and_salary_accessors():
    cfg = Config.from_yaml(SAMPLE)
    assert cfg.discount_rate.rate_at(5) == 0.045
    assert cfg.salary_increase_rate.rate_at(age=40) == 0.03


def test_invalid_convention_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.from_dict(
            {
                "valuation_date": "2025-12-31",
                "discount_rate": 0.05,
                "salary_increase_rate": 0.03,
                "service_day_count": "act/360",  # 허용되지 않는 값
            }
        )
