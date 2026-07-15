"""dateparse.py 테스트: 다양한 날짜 형식 자동 변환."""

import datetime as dt

import pytest

from dbo.dateparse import parse_flexible_date as P
from dbo.models import Employee


def test_excel_serial():
    assert P(42401) == dt.date(2016, 2, 1)


def test_yyyymmdd_int_and_str():
    assert P(20160201) == dt.date(2016, 2, 1)
    assert P("20190122") == dt.date(2019, 1, 22)


def test_separator_formats():
    assert P("2019-01-22") == dt.date(2019, 1, 22)
    assert P("2008/08/29") == dt.date(2008, 8, 29)
    assert P("2008.08.29") == dt.date(2008, 8, 29)
    assert P("2008. 08. 29.") == dt.date(2008, 8, 29)


def test_yymmdd_pivot():
    assert P("850501") == dt.date(1985, 5, 1)   # yy>=30 → 19yy
    assert P("050501") == dt.date(2005, 5, 1)   # yy<30 → 20yy


def test_datetime_and_blank():
    assert P(dt.datetime(2020, 12, 2)) == dt.date(2020, 12, 2)
    assert P(None) is None
    assert P("") is None
    assert P("nan") is None


def test_model_parses_mixed_formats():
    e = Employee(emp_id="1", birth_date="850501", gender="M", hire_date=20100301,
                 base_salary=3_000_000, current_year_accrual=5_000_000, emp_class="REGULAR",
                 interim_settlement_date="2018.01.01")
    assert e.birth_date == dt.date(1985, 5, 1)
    assert e.hire_date == dt.date(2010, 3, 1)
    assert e.interim_settlement_date == dt.date(2018, 1, 1)


def test_new_fields_optional():
    e = Employee(emp_id="1", birth_date="1985-05-01", gender="M", hire_date="2010-03-01",
                 base_salary=3_000_000, current_year_accrual=5_000_000, emp_class="REGULAR")
    assert e.next_year_accrual is None
    assert e.ifrs_enrolled is None
