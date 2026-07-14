"""명부 로딩·정제·검증.

명부 파일(xlsx/csv)을 읽어 표준 스키마 Employee 레코드로 변환하고,
계산 전에 데이터 품질을 검증한다. 검증은 오류(계산 중단)와 경고(플래그 후
진행)로 구분한 리포트를 생성한다.

검증 우선 원칙: 명부 입력은 계산 전에 반드시 검증한다.

주: 한글 컬럼명 매핑(고객별 양식 별칭)은 프롬프트 3에서 확장한다. 여기서는
표준 스키마(영문 필드명) 기준 로딩을 제공한다.
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd
import yaml
from pydantic import BaseModel, ValidationError

from .models import Employee

# 만 나이 이상치 경계 (프롬프트 1 검증 규칙)
MIN_PLAUSIBLE_AGE = 15   # 만 15세 미만: 이상치
MAX_PLAUSIBLE_AGE = 80   # 만 80세 초과: 이상치

# 표준 스키마 필수 컬럼
REQUIRED_COLUMNS = [
    "emp_id",
    "birth_date",
    "gender",
    "hire_date",
    "base_salary",
    "current_year_accrual",
    "emp_class",
]

# 표준 스키마 전체 필드(계산에 사용하는 컬럼). 이 밖의 컬럼은 계산에 불필요.
STANDARD_FIELDS = list(Employee.model_fields.keys())

# 내부 필드명(영문) → 사용자가 이해하는 명부 컬럼명(한글).
# 오류 메시지에 plan_type 같은 시스템 용어 대신 '제도구분'처럼 실제 컬럼명을 노출한다.
FIELD_LABELS = {
    "emp_id": "사원번호",
    "birth_date": "생년월일",
    "gender": "성별",
    "hire_date": "입사일자",
    "base_salary": "기준급여",
    "current_year_accrual": "당년도퇴직금추계액",
    "next_year_accrual": "차년도퇴직금추계액",
    "emp_class": "종업원구분",
    "interim_settlement_date": "중간정산기준일",
    "interim_settlement_amount": "중간정산액",
    "plan_type": "제도구분",
    "multiplier": "적용배수",
    "ifrs_enrolled": "IFRS가입",
}

# 필드별로 값이 잘못됐을 때 사용자에게 보여줄 '올바른 입력값' 안내(허용값 힌트).
_FIELD_INPUT_HINT = {
    "gender": "남자는 1, 여자는 2로 입력하세요.",
    "emp_class": "일반직 1, 임원 3, 계약직 4로 입력하세요.",
    "plan_type": "구분이 없으면 비워두거나 1, 간편법은 2, 제외는 3으로 입력하세요.",
    "birth_date": "yyyymmdd 형식으로 입력하세요 (예: 19900101).",
    "hire_date": "yyyymmdd 형식으로 입력하세요 (예: 20200101).",
    "interim_settlement_date": "yyyymmdd 형식으로 입력하세요 (예: 20230101).",
    "base_salary": "숫자(원)만 입력하세요.",
    "current_year_accrual": "숫자(원)만 입력하세요.",
    "next_year_accrual": "숫자(원)만 입력하세요.",
    "interim_settlement_amount": "숫자(원)만 입력하세요.",
}


def _friendly_parse_message(loc: str, raw_value) -> str:
    """파싱/검증 오류를 사용자용 한글 메시지로 변환.

    시스템 필드명(plan_type 등) 대신 명부 컬럼명(제도구분)을 쓰고,
    허용값 힌트를 덧붙여 기업 담당자가 바로 고칠 수 있게 한다.
    """
    label = FIELD_LABELS.get(loc, loc)
    shown = "빈칸" if raw_value is None or raw_value == "" else f"'{raw_value}'"
    hint = _FIELD_INPUT_HINT.get(loc, "입력값을 확인해 주세요.")
    return f"[{label}] 입력값 {shown}을(를) 확인해 주세요 — {hint}"

# 업로드 금지/불필요 민감 컬럼 패턴 (부분일치, 소문자 기준).
# (키워드들, 라벨, 안내메시지) — 실명·주민번호는 반드시 제거, 나머지는 불필요 정보.
SENSITIVE_PATTERNS = [
    (["성명", "성함", "이름", "명(한글", "korean_name", "fullname", "full_name"],
     "실명", "성명(실명)은 수집하지 않습니다. 사번만 사용하니 컬럼을 삭제하고 다시 올려주세요."),
    (["주민", "주민번호", "주민등록", "ssn", "rrn", "생년월일주민"],
     "주민등록번호", "주민등록번호는 수집하지 않습니다. 컬럼을 삭제하고 다시 올려주세요."),
    (["전화", "연락처", "휴대", "핸드폰", "phone", "mobile", "tel", "hp"],
     "연락처", "계산에 불필요한 개인정보입니다. 컬럼을 삭제하고 다시 올려주세요."),
    (["주소", "거주", "address"],
     "주소", "계산에 불필요한 개인정보입니다. 컬럼을 삭제하고 다시 올려주세요."),
    (["이메일", "메일", "email", "e-mail"],
     "이메일", "계산에 불필요한 개인정보입니다. 컬럼을 삭제하고 다시 올려주세요."),
    (["계좌", "은행", "account", "bank"],
     "계좌정보", "계산에 불필요한 개인정보입니다. 컬럼을 삭제하고 다시 올려주세요."),
    (["여권", "passport", "면허", "license", "신분증"],
     "신분증정보", "계산에 불필요한 개인정보입니다. 컬럼을 삭제하고 다시 올려주세요."),
    (["name"],
     "실명", "성명(실명)은 수집하지 않습니다. 사번만 사용하니 컬럼을 삭제하고 다시 올려주세요."),
]


def detect_sensitive_columns(columns) -> List[Tuple[str, str, str]]:
    """민감/불필요 컬럼을 검출한다. 반환: [(컬럼명, 라벨, 안내메시지), ...]

    표준 스키마 컬럼은 제외한다. 성별 등 표준 컬럼이 오탐되지 않도록 화이트리스트 우선.
    """
    std = {f.lower() for f in STANDARD_FIELDS}
    found = []
    for col in columns:
        cl = str(col).strip().lower()
        if cl in std:
            continue
        for keys, label, msg in SENSITIVE_PATTERNS:
            if any(k in cl for k in keys):
                found.append((str(col), label, msg))
                break
    return found


class Severity(str, Enum):
    ERROR = "ERROR"      # 계산 중단
    WARNING = "WARNING"  # 플래그 후 진행


class ValidationIssue(BaseModel):
    """개별 검증 이슈."""

    emp_id: Optional[str]  # 행 단위로 사번 미상일 수 있어 nullable
    row: Optional[int]     # 원본 명부 행 번호(0-based, 헤더 제외)
    rule: str              # 위반 규칙 식별자
    severity: Severity
    message: str


class ValidationReport(BaseModel):
    """검증 리포트."""

    issues: List[ValidationIssue] = []
    n_records: int = 0

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def flagged_emp_ids(self) -> set:
        """경고 플래그가 붙은 사번 집합(계산은 진행하되 표시)."""
        return {i.emp_id for i in self.warnings if i.emp_id is not None}

    def add(
        self,
        rule: str,
        severity: Severity,
        message: str,
        emp_id: Optional[str] = None,
        row: Optional[int] = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                emp_id=emp_id, row=row, rule=rule, severity=severity, message=message
            )
        )

    def summary(self) -> str:
        return (
            f"레코드 {self.n_records}건 | "
            f"오류 {len(self.errors)}건 | 경고 {len(self.warnings)}건"
        )


def _read_dataframe(path: Union[str, Path]) -> pd.DataFrame:
    """xlsx/csv 명부 파일을 DataFrame으로 읽는다.

    양식에 '작성요령' 안내 시트가 포함되어 있어도(사용자가 그대로 업로드해도)
    작성요령 시트는 건너뛰고 실제 명부(데이터) 시트만 읽는다.
    """
    path = Path(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        xls = pd.ExcelFile(path)
        sheet = next(
            (s for s in xls.sheet_names if "요령" not in str(s)),
            xls.sheet_names[0],
        )
        return xls.parse(sheet)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"지원하지 않는 명부 파일 형식: {path.suffix}")


def load_column_map(path: Union[str, Path]) -> dict:
    """컬럼 매핑 YAML을 로드한다 (고객별 한글 컬럼명 별칭)."""
    with Path(path).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _emp_id_to_str(v) -> Optional[str]:
    """사번을 문자열로 정규화 (숫자형의 '.0' 꼬리 제거, 앞뒤 공백 제거)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


_BRACKET_RE = re.compile(r"[\(（【\[].*", re.S)   # 첫 여는 괄호부터 끝까지(주석)


def _norm_header(s) -> str:
    """헤더명 정규화: 괄호 주석 제거 + 공백(줄바꿈) 제거 → 별칭 매칭 안정화.

    실제 고객 양식은 헤더에 줄바꿈과 괄호 설명을 붙인다(예: '성별\\n(1:남자, 2:여자)',
    '당년도\\n퇴직금추계액', '직종구분\\n(1:직원…6:임원(PUC)…)'). 주석은 항상 이름 뒤에
    오므로 첫 여는 괄호부터 잘라내고(중첩 괄호도 함께 제거), 남은 줄바꿈·공백을 없애
    '성별'·'당년도퇴직금추계액'·'직종구분'으로 표준화해 별칭과 대조한다.
    """
    s = _BRACKET_RE.sub("", str(s))        # 첫 괄호(주석 시작)부터 전부 제거
    s = re.sub(r"\s+", "", s)              # 줄바꿈 포함 모든 공백 제거
    return s.strip().lower()


def _value_lookup(x, value_map: dict):
    """값 매핑 조회 — 숫자코드(1/2)·'1.0'·문자열을 모두 견고하게 대조."""
    if x in value_map:
        return value_map[x]
    xs = _emp_id_to_str(x)                 # 1.0 → '1', 공백 정리
    if xs is not None and xs in value_map:
        return value_map[xs]
    return x


def apply_column_map(df: pd.DataFrame, column_map: dict) -> pd.DataFrame:
    """소스 DataFrame을 표준 스키마 컬럼명/값으로 매핑한다.

    column_map 구조:
      columns: { 표준필드: 소스컬럼명 또는 [별칭들] }
      values:  { gender|emp_class|plan_type: { 소스값: 표준값 } }  # 선택
    별칭은 헤더 정규화(_norm_header) 후 대조하므로 줄바꿈·괄호 설명이 있어도 매칭된다.
    값 매핑은 지정된 컬럼에만 적용하며 숫자코드도 견고하게 대조한다.
    """
    df = df.copy()
    columns: Dict[str, Union[str, list]] = column_map.get("columns", {})
    # 정규화한 헤더 → 원본 컬럼명 (선두 우선)
    norm_to_col: Dict[str, str] = {}
    for c in df.columns:
        norm_to_col.setdefault(_norm_header(c), c)
    for std_field, aliases in columns.items():
        if isinstance(aliases, str):
            aliases = [aliases]
        if std_field in df.columns:        # 이미 표준명이면 그대로 사용
            continue
        for alias in aliases:
            src = norm_to_col.get(_norm_header(alias))
            if src is not None and src in df.columns and src != std_field:
                df = df.rename(columns={src: std_field})
                break

    values: Dict[str, dict] = column_map.get("values", {})
    for col, value_map in values.items():
        if col in df.columns and value_map:
            df[col] = df[col].map(lambda x: _value_lookup(x, value_map))

    if "emp_id" in df.columns:
        df["emp_id"] = df["emp_id"].map(_emp_id_to_str)
    return df


def load_census(
    path: Union[str, Path],
    column_map: Optional[Union[str, Path, dict]] = None,
) -> Tuple[List[Employee], ValidationReport, pd.DataFrame]:
    """명부 파일을 로드해 (레코드 리스트, 검증 리포트, 매핑된 DataFrame) 반환.

    column_map: 컬럼 매핑 YAML 경로 또는 이미 로드된 dict. 지정 시 표준 스키마로 매핑.
    파싱/타입 오류는 리포트에 ERROR로 기록되며 해당 행은 레코드 리스트에서 제외된다.
    도메인 검증(입사일/생년월일 등)은 records_from_dataframe → validate_census 순으로 수행한다.
    """
    df = _read_dataframe(path)
    if column_map is not None:
        cmap = column_map if isinstance(column_map, dict) else load_column_map(column_map)
        df = apply_column_map(df, cmap)
    elif "emp_id" in df.columns:
        df = df.copy()
        df["emp_id"] = df["emp_id"].map(_emp_id_to_str)
    records, report = records_from_dataframe(df)
    return records, report, df


def records_from_dataframe(
    df: pd.DataFrame,
) -> Tuple[List[Employee], ValidationReport]:
    """DataFrame → Employee 레코드 변환. 민감컬럼·필수컬럼·파싱 검증을 수행한다."""
    report = ValidationReport(n_records=len(df))

    # (1) 실명·민감·불필요 컬럼 검출 → 오류(제출 차단) + 재편집 안내
    sensitive = detect_sensitive_columns(df.columns)
    for col, label, msg in sensitive:
        report.add(
            rule="sensitive_column",
            severity=Severity.ERROR,
            message=f"'{col}' 컬럼({label})은 올리면 안 됩니다 — {msg}",
        )

    # (2) 표준 스키마 외 컬럼은 계산에 불필요 → 경고 후 제거(민감 컬럼 포함).
    sensitive_names = {s[0] for s in sensitive}
    unknown = [c for c in df.columns if str(c).strip() not in STANDARD_FIELDS]
    ignorable = [c for c in unknown if c not in sensitive_names]
    if ignorable:
        report.add(
            rule="ignored_columns",
            severity=Severity.WARNING,
            message=f"계산에 사용하지 않아 무시되는 컬럼: {ignorable}",
        )
    # 표준 컬럼만 남겨 파싱(불필요/민감 컬럼 제거 → 뒤 파싱 오류 방지)
    df = df[[c for c in df.columns if str(c).strip() in STANDARD_FIELDS]]

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        report.add(
            rule="missing_columns",
            severity=Severity.ERROR,
            message=f"필수 컬럼 누락: {missing_cols}",
        )
        return [], report

    records: List[Employee] = []
    for row_idx, row in df.iterrows():
        raw = row.to_dict()
        # NaN → None 정규화 (nullable 필드 처리)
        clean = {k: (None if pd.isna(v) else v) for k, v in raw.items()}
        emp_id_val = clean.get("emp_id")
        emp_id_str = None if emp_id_val is None else str(emp_id_val).strip()
        try:
            records.append(Employee.model_validate(clean))
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                field = str(err["loc"][0]) if err["loc"] else loc
                report.add(
                    rule=f"parse:{loc}",
                    severity=Severity.ERROR,
                    message=_friendly_parse_message(field, clean.get(field)),
                    emp_id=emp_id_str,
                    row=int(row_idx),
                )
    return records, report


def _years_between(start: date, end: date) -> float:
    """단순 만 나이/근속 계산용 연수(act/365.25 근사)."""
    return (end - start).days / 365.25


def validate_census(
    records: List[Employee],
    valuation_date: date,
    report: Optional[ValidationReport] = None,
) -> ValidationReport:
    """도메인 검증 규칙 적용.

    오류(계산 중단):
      - 입사일 > 기준일 (미래 입사)
      - 생년월일 이상치: 만 15세 미만
      - 중간정산기준일 < 입사일
      - 기준급여 <= 0
      - 생년월일 >= 입사일 (출생 후 입사 위배)

    경고(플래그 후 진행):
      - 생년월일 이상치: 만 80세 초과
      - 중간정산기준일 있는데 중간정산액 누락(또는 반대)
      - 당년도추계액 < 0
      - 적용배수 <= 0
    """
    if report is None:
        report = ValidationReport(n_records=len(records))

    for emp in records:
        eid = emp.emp_id

        # 입사일 > 기준일
        if emp.hire_date > valuation_date:
            report.add(
                rule="hire_after_valuation",
                severity=Severity.ERROR,
                message=f"입사일({emp.hire_date})이 산출기준일({valuation_date}) 이후",
                emp_id=eid,
            )

        # 생년월일 vs 입사일
        if emp.birth_date >= emp.hire_date:
            report.add(
                rule="birth_after_hire",
                severity=Severity.ERROR,
                message=f"생년월일({emp.birth_date})이 입사일({emp.hire_date}) 이후/동일",
                emp_id=eid,
            )

        # 나이 이상치
        age = _years_between(emp.birth_date, valuation_date)
        if age < MIN_PLAUSIBLE_AGE:
            report.add(
                rule="age_below_min",
                severity=Severity.ERROR,
                message=f"기준일 만 나이 {age:.1f}세 (< {MIN_PLAUSIBLE_AGE})",
                emp_id=eid,
            )
        elif age > MAX_PLAUSIBLE_AGE:
            report.add(
                rule="age_above_max",
                severity=Severity.WARNING,
                message=f"기준일 만 나이 {age:.1f}세 (> {MAX_PLAUSIBLE_AGE})",
                emp_id=eid,
            )

        # 중간정산기준일 < 입사일
        if emp.interim_settlement_date is not None:
            if emp.interim_settlement_date < emp.hire_date:
                report.add(
                    rule="interim_before_hire",
                    severity=Severity.ERROR,
                    message=(
                        f"중간정산기준일({emp.interim_settlement_date})이 "
                        f"입사일({emp.hire_date}) 이전"
                    ),
                    emp_id=eid,
                )
            if emp.interim_settlement_date > valuation_date:
                report.add(
                    rule="interim_after_valuation",
                    severity=Severity.ERROR,
                    message=(
                        f"중간정산기준일({emp.interim_settlement_date})이 "
                        f"산출기준일({valuation_date}) 이후"
                    ),
                    emp_id=eid,
                )
            if emp.interim_settlement_amount is None:
                report.add(
                    rule="interim_amount_missing",
                    severity=Severity.WARNING,
                    message="중간정산기준일은 있으나 중간정산액이 누락됨",
                    emp_id=eid,
                )
        elif emp.interim_settlement_amount is not None:
            report.add(
                rule="interim_date_missing",
                severity=Severity.WARNING,
                message="중간정산액은 있으나 중간정산기준일이 누락됨",
                emp_id=eid,
            )

        # 기준급여
        if emp.base_salary <= 0:
            report.add(
                rule="base_salary_nonpositive",
                severity=Severity.ERROR,
                message=f"기준급여 <= 0 ({emp.base_salary})",
                emp_id=eid,
            )

        # 당년도추계액
        if emp.current_year_accrual < 0:
            report.add(
                rule="accrual_negative",
                severity=Severity.WARNING,
                message=f"당년도추계액 < 0 ({emp.current_year_accrual})",
                emp_id=eid,
            )

        # 적용배수
        if emp.multiplier <= 0:
            report.add(
                rule="multiplier_nonpositive",
                severity=Severity.WARNING,
                message=f"적용배수 <= 0 ({emp.multiplier})",
                emp_id=eid,
            )

    return report
