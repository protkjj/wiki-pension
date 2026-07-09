"""유연한 날짜 파싱 — 고객 명부의 다양한 날짜 형식을 자동 인식.

지원 형식 (계리사 '날짜형식 변환' 시트 참고):
  - date / datetime / pandas Timestamp
  - 엑셀 시리얼 정수 (예: 42401 → 2016-02-01)
  - YYYYMMDD 정수/문자 (예: 20160201)
  - YYMMDD (예: 850501 → 1985-05-01, 2자리 연도 피벗 30)
  - 구분자 형식: 2019-01-22, 2008/08/29, 2008.08.29, "2008. 08. 29." 등
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

_EXCEL_EPOCH = date(1899, 12, 30)   # 엑셀 시리얼 0일 기준(1900 윤년 버그 포함)
_YEAR_PIVOT = 30                    # 2자리 연도: yy>=30 → 19yy, 아니면 20yy


def _fix_2digit_year(y: int) -> int:
    if y < 100:
        return 1900 + y if y >= _YEAR_PIVOT else 2000 + y
    return y


def _from_number(n: float) -> Optional[date]:
    """정수형 날짜 판별: YYYYMMDD / YYMMDD / 엑셀 시리얼."""
    i = int(round(n))
    if i >= 1_000_000:                      # YYYYMMDD (8자리)
        y, md = divmod(i, 10000)
        m, d = divmod(md, 100)
        return date(y, m, d)
    if 100_00 <= i <= 999_99:               # 5자리대: 엑셀 시리얼(1900~2170)
        return _EXCEL_EPOCH + timedelta(days=i)
    if 10_000 <= i < 100_000:               # 5자리: 엑셀 시리얼
        return _EXCEL_EPOCH + timedelta(days=i)
    if 100_000 <= i < 1_000_000:            # 6자리: YYMMDD
        y, md = divmod(i, 10000)
        m, d = divmod(md, 100)
        return date(_fix_2digit_year(y), m, d)
    if 0 < i < 10_000:                      # 소수 시리얼(오래된 날짜)
        return _EXCEL_EPOCH + timedelta(days=i)
    raise ValueError(f"날짜로 해석 불가한 숫자: {n}")


def parse_flexible_date(value) -> Optional[date]:
    """다양한 형식을 date로 변환. 빈값은 None. 해석 실패 시 ValueError."""
    if value is None:
        return None
    # pandas NaT/NaN 처리
    try:
        import pandas as pd  # 지연 import
        if value is pd.NaT or (isinstance(value, float) and pd.isna(value)):
            return None
        if isinstance(value, pd.Timestamp):
            return value.date()
    except Exception:  # noqa: BLE001
        pass

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return _from_number(value)

    s = str(value).strip()
    if not s or s.lower() in {"nan", "nat", "none"}:
        return None

    # 구분자(-, /, ., 공백) 형식
    if re.search(r"[-/.]", s):
        parts = [p for p in re.split(r"[-/.\s]+", s) if p != ""]
        if len(parts) >= 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            return date(_fix_2digit_year(y), m, d)

    # 숫자만
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    if len(digits) == 6:
        return date(_fix_2digit_year(int(digits[:2])), int(digits[2:4]), int(digits[4:6]))
    if digits:
        return _from_number(float(digits))

    # 마지막 시도: ISO
    return date.fromisoformat(s)
