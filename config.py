"""계산 설정 모델 (pydantic v2 + YAML 로더).

설정 기반 원칙: 계산에 영향을 주는 모든 가정은 YAML로 제어한다.
기본값은 모델에 명시하고 문서화한다.

할인율은 단일값이지만 향후 커브(기간별)로, 임금상승률은 단일값이지만
연령별/근속별 테이블로 확장 가능한 구조로 둔다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Dict, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import EmpClass

# ---------------------------------------------------------------------------
# 할인율: 단일값 + 커브 확장 가능 구조
# ---------------------------------------------------------------------------


class DiscountRate(BaseModel):
    """할인율. 현재는 단일값(flat)만 지원하되, 기간별 커브로 확장 가능한 구조."""

    model_config = ConfigDict(extra="forbid")

    flat: float = Field(..., description="단일 할인율 (예: 0.045 = 4.5%)")
    # 향후: curve: Optional[Dict[float, float]] = None  # {기간(연): 할인율}

    def rate_at(self, t: float) -> float:  # noqa: ARG002 - 커브 확장 대비 시그니처
        """t시점(연) 할인율. 현재는 flat 값을 반환."""
        return self.flat


class SalaryIncreaseRate(BaseModel):
    """임금상승률. 현재는 단일값만 지원하되, 연령별/근속별 테이블로 확장 가능한 구조."""

    model_config = ConfigDict(extra="forbid")

    flat: float = Field(..., description="단일 임금상승률 (예: 0.03 = 3%)")
    # 향후: by_age: Optional[Dict[int, float]] = None
    #       by_service: Optional[Dict[int, float]] = None

    def rate_at(self, age: Optional[int] = None, service: Optional[int] = None) -> float:  # noqa: ARG002
        """도달연령/근속 기준 임금상승률. 현재는 flat 값을 반환."""
        return self.flat


# ---------------------------------------------------------------------------
# 정년: 종업원구분별 지정 가능
# ---------------------------------------------------------------------------


class RetirementAge(BaseModel):
    """정년. 종업원구분별로 다르게 지정 가능. 미지정 구분은 default 적용."""

    model_config = ConfigDict(extra="forbid")

    default: int = Field(60, description="기본 정년")
    by_class: Dict[EmpClass, int] = Field(
        default_factory=dict, description="종업원구분별 정년 (미지정 시 default)"
    )

    def for_class(self, emp_class: EmpClass) -> int:
        return self.by_class.get(emp_class, self.default)


# ---------------------------------------------------------------------------
# 계산 convention 리터럴 타입
# ---------------------------------------------------------------------------

DecrementTiming = Literal["end_of_year", "mid_year"]
SalaryIncreaseTiming = Literal["start_of_year", "mid_year", "end_of_year"]
DiscountTiming = Literal["end_of_year", "mid_year"]
ServiceDayCount = Literal["act/365", "act/365.25", "months"]
RetirementRateBasis = Literal["age", "service"]
# 당기근무원가 산출 convention 선택지 (프롬프트 2에서 각 방식 구현).
CSCMethod = Literal["one_year_slice", "attained_minus_prior"]


class DecrementTableConfig(BaseModel):
    """탈퇴율 테이블 파일 경로 설정."""

    model_config = ConfigDict(extra="forbid")

    retirement_by_age: Optional[str] = Field(
        "decrement_tables/retirement_rates_age.csv",
        description="연령별 퇴직률 CSV (retirement_rate_basis=age일 때)",
    )
    retirement_by_service: Optional[str] = Field(
        "decrement_tables/retirement_rates_service.csv",
        description="근속별 퇴직률 CSV (retirement_rate_basis=service일 때)",
    )
    mortality: Optional[str] = Field(
        "decrement_tables/mortality.csv",
        description="성별·연령별 사망률 CSV",
    )


class Config(BaseModel):
    """계산 설정 전체."""

    model_config = ConfigDict(extra="forbid")

    valuation_date: date = Field(..., description="산출기준일")

    discount_rate: DiscountRate = Field(..., description="할인율")
    salary_increase_rate: SalaryIncreaseRate = Field(..., description="임금상승률")
    retirement_age: RetirementAge = Field(
        default_factory=RetirementAge, description="정년 (종업원구분별)"
    )

    decrement_timing: DecrementTiming = Field(
        "end_of_year", description="탈퇴 발생 시점 가정"
    )
    salary_increase_timing: SalaryIncreaseTiming = Field(
        "start_of_year", description="임금상승 반영 시점 가정"
    )
    discount_timing: DiscountTiming = Field(
        "end_of_year", description="탈퇴시점까지 할인 기간 산정 방식 (탈퇴 timing과 정합)"
    )
    service_day_count: ServiceDayCount = Field(
        "act/365", description="근속 일할 방식"
    )
    retirement_rate_basis: RetirementRateBasis = Field(
        "age", description="퇴직률 적용 기준"
    )
    csc_method: CSCMethod = Field(
        "one_year_slice", description="당기근무원가 산출 convention"
    )

    rounding: int = Field(1, description="최종 반올림 단위 (원, 기본 1)")

    decrement_tables: DecrementTableConfig = Field(
        default_factory=DecrementTableConfig, description="탈퇴율 테이블 경로"
    )

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "Config":
        """YAML 파일에서 설정을 로드한다.

        단일값 축약 표기 지원:
          discount_rate: 0.045            -> {flat: 0.045}
          salary_increase_rate: 0.03      -> {flat: 0.03}
          retirement_age: 60              -> {default: 60}
        """
        path = Path(path)
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        """dict에서 설정을 로드하며 단일값 축약 표기를 정규화한다."""
        data = dict(raw)

        # 스칼라 축약 표기 정규화
        if isinstance(data.get("discount_rate"), (int, float)):
            data["discount_rate"] = {"flat": float(data["discount_rate"])}
        if isinstance(data.get("salary_increase_rate"), (int, float)):
            data["salary_increase_rate"] = {"flat": float(data["salary_increase_rate"])}
        if isinstance(data.get("retirement_age"), int):
            data["retirement_age"] = {"default": data["retirement_age"]}

        return cls.model_validate(data)
