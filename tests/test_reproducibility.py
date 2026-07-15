"""계산 재현성 테스트.

절대 원칙 1(재현성): 동일 입력 + 동일 config → 항상 동일한 산출물.
실행시각은 로그의 메타데이터로만 기록되고 계산·결과 파일에는 개입하지 않는다.
"""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generate_sample_census import generate  # noqa: E402

from dbo.census import load_census, validate_census  # noqa: E402
from dbo.config import Config  # noqa: E402
from dbo.decrement import DecrementTables  # noqa: E402
from dbo.engine import calculate_census  # noqa: E402
from dbo.outputs import write_outputs  # noqa: E402

CONFIG_PATH = "config/assumptions_sample.yaml"
COLMAP_PATH = "config/column_map_sample.yaml"
FIXED_TS = "2025-12-31T00:00:00+00:00"


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def census_file(tmp_path):
    path = tmp_path / "census.xlsx"
    generate(n=300, seed=11).to_excel(path, index=False)
    return path


def _load(census_file):
    config = Config.from_yaml(CONFIG_PATH)
    tables = DecrementTables.from_config(config, base_dir="config")
    records, report, _ = load_census(census_file, column_map=COLMAP_PATH)
    validate_census(records, config.valuation_date, report)
    return records, report, config, tables


def test_calculation_is_deterministic(census_file):
    """두 번 계산해도 총계·개인별 DBO/CSC가 완전히 동일."""
    records, _, config, tables = _load(census_file)
    r1 = calculate_census(records, config, tables, with_detail=False)
    r2 = calculate_census(records, config, tables, with_detail=False)

    assert r1.total_dbo == r2.total_dbo
    assert r1.total_csc == r2.total_csc
    assert [(x.emp_id, x.dbo, x.csc) for x in r1.results] == [
        (x.emp_id, x.dbo, x.csc) for x in r2.results
    ]


def test_output_files_are_byte_identical(tmp_path, census_file):
    """같은 입력으로 2회 실행 시 결과 파일(xlsx, run_log.json) 바이트 동일.

    run_log의 실행시각은 고정 타임스탬프를 주입해 메타데이터 비결정성을 제거.
    """
    records, report, config, tables = _load(census_file)

    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    r = calculate_census(records, config, tables, with_detail=False)
    p1 = write_outputs(out1, records, r, config, tables, census_path=census_file, report=report, timestamp=FIXED_TS)
    p2 = write_outputs(out2, records, r, config, tables, census_path=census_file, report=report, timestamp=FIXED_TS)

    assert _sha256(p1["xlsx"]) == _sha256(p2["xlsx"])
    assert _sha256(p1["run_log"]) == _sha256(p2["run_log"])


def test_xlsx_has_no_wallclock_timestamp(tmp_path, census_file):
    """xlsx 내부 문서 메타에 실행시각(벽시계)이 새지 않는지 확인.

    두 프로세스가 다른 초에 실행돼도 바이트가 같으려면, core.xml의 생성/수정
    시각이 고정값이어야 한다(그렇지 않으면 초 단위 차이로 파일이 달라짐).
    """
    import zipfile

    records, report, config, tables = _load(census_file)
    r = calculate_census(records, config, tables, with_detail=False)
    p = write_outputs(tmp_path / "o", records, r, config, tables,
                      census_path=census_file, report=report, timestamp=FIXED_TS)
    with zipfile.ZipFile(p["xlsx"]) as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    assert "2000-01-01T00:00:00Z" in core          # 고정 시각으로 치환됨
    assert "2025-" not in core and "2026-" not in core   # 현재연도 시각 미포함
