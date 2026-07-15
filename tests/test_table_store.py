"""table_store.py 테스트: 컬럼 정규화, 버전 저장·목록·로딩."""

import shutil
from pathlib import Path

import pandas as pd
import pytest

from dbo.config import Config
from dbo.decrement import DecrementTables
from dbo.models import Gender
from dbo import table_store as ts


@pytest.fixture
def config_dir(tmp_path):
    """기본 decrement_tables 3종을 갖춘 임시 config 디렉토리."""
    base = tmp_path / "decrement_tables"
    base.mkdir(parents=True)
    pd.DataFrame({"age": [30, 40, 50], "rate": [0.05, 0.03, 0.04]}).to_csv(
        base / "retirement_rates_age.csv", index=False)
    pd.DataFrame({"service": [0, 5, 10], "rate": [0.1, 0.05, 0.03]}).to_csv(
        base / "retirement_rates_service.csv", index=False)
    pd.DataFrame({"age": [30, 40, 50], "male_qx": [0.001, 0.002, 0.004],
                  "female_qx": [0.0006, 0.0012, 0.0024]}).to_csv(
        base / "mortality.csv", index=False)
    return tmp_path


def test_normalize_table_accepts_korean_headers():
    # 한글 헤더 → 표준 컬럼명으로 정규화
    df = pd.DataFrame({"연령": [30, 40], "퇴직율": [0.05, 0.03]})
    out = ts.normalize_table(df, "retirement_by_age")
    assert list(out.columns) == ["age", "rate"]
    assert out["rate"].tolist() == [0.05, 0.03]


def test_normalize_table_missing_column_raises():
    df = pd.DataFrame({"age": [30], "wrong": [0.05]})
    with pytest.raises(ValueError):
        ts.normalize_table(df, "retirement_by_age")


def test_save_and_list_version(config_dir):
    assert ts.list_versions(config_dir) == []
    # 연령별 퇴직률만 업로드 → 나머지는 기본에서 복사되어 완결
    new_age = pd.DataFrame({"연령": [30, 40, 50], "퇴직률": [0.07, 0.05, 0.06]})
    dest = ts.save_version(config_dir, "2025", {"retirement_by_age": new_age},
                           description="2025년 경험률", created="2025-12-31")
    assert dest.exists()
    assert ts.list_versions(config_dir) == ["2025"]
    # 3종 파일 모두 존재(빠진 건 기본 복사)
    for fname in ts.STD_FILES.values():
        assert (dest / fname).exists()
    assert ts.version_meta(config_dir, "2025")["description"] == "2025년 경험률"


def test_saved_version_loads_via_config(config_dir):
    new_age = pd.DataFrame({"age": [30, 40, 50], "rate": [0.09, 0.09, 0.09]})
    ts.save_version(config_dir, "2025", {"retirement_by_age": new_age})

    # config의 decrement_tables를 버전 경로로 설정 → 그 버전 테이블이 로드되는지
    paths = ts.relative_paths("2025")
    cfg = Config.from_dict({
        "valuation_date": "2025-12-31", "discount_rate": 0.045,
        "salary_increase_rate": 0.03,
        "decrement_tables": paths,
    })
    tables = DecrementTables.from_config(cfg, base_dir=str(config_dir))
    # 2025 버전의 값(0.09) 이 조회돼야 함
    assert tables.retirement_rate_by_age(40) == pytest.approx(0.09)
    # 사망률은 기본에서 복사된 값
    assert tables.mortality_rate(40, Gender.M) == pytest.approx(0.002)


def test_relative_paths_default_vs_version():
    assert ts.relative_paths("기본")["mortality"] == "decrement_tables/mortality.csv"
    assert ts.relative_paths("2025")["mortality"] == "decrement_tables/versions/2025/mortality.csv"


def test_delete_version(config_dir):
    ts.save_version(config_dir, "tmp", {})
    assert "tmp" in ts.list_versions(config_dir)
    assert ts.delete_version(config_dir, "tmp") is True
    assert "tmp" not in ts.list_versions(config_dir)
