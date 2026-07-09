"""데이터 모델 (pydantic v2).

종업원 명부 레코드의 표준 스키마를 정의한다.
개인정보 보호 원칙: 성명 등 직접 식별정보는 받지 않는다(사번만 사용).
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .dateparse import parse_flexible_date


class Gender(str, Enum):
    """성별."""

    M = "M"
    F = "F"


class EmpClass(str, Enum):
    """종업원구분."""

    EXECUTIVE = "EXECUTIVE"  # 임원
    REGULAR = "REGULAR"      # 정규직
    CONTRACT = "CONTRACT"    # 계약직


class PlanType(int, Enum):
    """제도구분.

    1 = DB 정상평가 (PUC 수식으로 계산)
    2 = 간편법 (당년도추계액을 그대로 부채로 계상)
    3 = 제외 (결과에서 제외하되 별도 목록으로 출력)
    """

    DB_NORMAL = 1
    SIMPLIFIED = 2
    EXCLUDED = 3


class Employee(BaseModel):
    """종업원 명부 레코드 (표준 스키마).

    필드명은 영문 스네이크케이스, 도메인 의미는 한글 주석으로 병기한다.
    """

    model_config = ConfigDict(
        extra="forbid",           # 스키마 밖 필드는 오류로 처리
        use_enum_values=False,    # Enum 인스턴스를 유지 (비교/검증 편의)
        validate_assignment=True,
    )

    emp_id: str = Field(..., description="사번 (성명은 받지 않는다)")
    birth_date: date = Field(..., description="생년월일")
    gender: Gender = Field(..., description="성별 (M/F)")
    hire_date: date = Field(..., description="입사일")
    base_salary: float = Field(..., description="기준급여 = 월평균임금 (원)")
    current_year_accrual: float = Field(
        ..., description="당년도추계액 (원): 기준일 현재 일시 지급 시 퇴직금"
    )
    next_year_accrual: Optional[float] = Field(
        None, description="차년도추계액 (원, nullable): 차기말 기준 예상 퇴직금"
    )
    ifrs_enrolled: Optional[str] = Field(
        None, description="IFRS 가입 여부 (Y/N, nullable). 값 검증은 census/actuary_checks에서"
    )
    emp_class: EmpClass = Field(..., description="종업원구분")
    interim_settlement_date: Optional[date] = Field(
        None, description="중간정산기준일 (nullable)"
    )
    interim_settlement_amount: Optional[float] = Field(
        None, description="중간정산액 (원, nullable)"
    )
    plan_type: PlanType = Field(
        PlanType.DB_NORMAL, description="제도구분 (1=DB정상, 2=간편법, 3=제외)"
    )
    multiplier: float = Field(1.0, description="적용배수 (기본 1.0)")

    @field_validator("birth_date", "hire_date", "interim_settlement_date", mode="before")
    @classmethod
    def _flex_date(cls, v):
        """다양한 날짜 형식(YYYYMMDD·슬래시·점·엑셀시리얼 등)을 date로 자동 변환."""
        try:
            return parse_flexible_date(v)
        except Exception:  # noqa: BLE001 - 해석 실패 시 원값을 넘겨 pydantic이 검증 오류를 내게 함
            return v

    @model_validator(mode="after")
    def _normalize_emp_id(self) -> "Employee":
        # 사번은 공백을 제거해 조인 안정성 확보. 형식적 정제만 수행(값 검증은 census에서).
        object.__setattr__(self, "emp_id", str(self.emp_id).strip())
        return self

    def service_start_date(self) -> date:
        """근속 기산일. 중간정산자는 중간정산기준일부터 근속을 다시 기산한다."""
        if self.interim_settlement_date is not None:
            return self.interim_settlement_date
        return self.hire_date
