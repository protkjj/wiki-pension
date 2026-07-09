"""탈퇴율 테이블 버전 저장소.

퇴직률·사망률 테이블을 버전(예: 연도)별 폴더로 관리한다.

레이아웃:
  config/decrement_tables/
    retirement_rates_age.csv          # '기본' 버전 (하위호환)
    retirement_rates_service.csv
    mortality.csv
    versions/
      2025/  retirement_rates_age.csv, retirement_rates_service.csv, mortality.csv, meta.json
      2024/  ...

각 버전 폴더는 표준 파일명 3종을 갖는다. 업로드 시 일부만 주면 나머지는
기준(base) 버전에서 복사해 항상 완결된 버전이 되도록 한다.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd

# 테이블 종류 → 표준 파일명
STD_FILES = {
    "retirement_by_age": "retirement_rates_age.csv",
    "retirement_by_service": "retirement_rates_service.csv",
    "mortality": "mortality.csv",
}

# 종류별 필수 컬럼(표준명)
REQUIRED_COLS = {
    "retirement_by_age": ["age", "rate"],
    "retirement_by_service": ["service", "rate"],
    "mortality": ["age", "male_qx", "female_qx"],
}

# 업로드 파일의 한글/변형 헤더 → 표준 컬럼명 별칭
COLUMN_ALIASES = {
    "연령": "age", "나이": "age", "만나이": "age",
    "근속": "service", "근속연수": "service", "근속년수": "service",
    "퇴직률": "rate", "퇴직율": "rate", "비율": "rate", "확률": "rate",
    "남자": "male_qx", "남": "male_qx", "남성": "male_qx", "남자사망률": "male_qx", "male": "male_qx",
    "여자": "female_qx", "여": "female_qx", "여성": "female_qx", "여자사망률": "female_qx", "female": "female_qx",
}


def versions_dir(config_dir: Union[str, Path]) -> Path:
    return Path(config_dir) / "decrement_tables" / "versions"


def base_dir(config_dir: Union[str, Path]) -> Path:
    return Path(config_dir) / "decrement_tables"


def list_versions(config_dir: Union[str, Path]) -> List[str]:
    """등록된 버전 이름 목록('기본' 제외)."""
    d = versions_dir(config_dir)
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def version_meta(config_dir: Union[str, Path], name: str) -> dict:
    p = versions_dir(config_dir) / name / "meta.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def relative_paths(version: str) -> Dict[str, str]:
    """config 디렉토리 기준 상대경로 dict (config.decrement_tables 에 그대로 사용)."""
    if version == "기본":
        return {k: f"decrement_tables/{v}" for k, v in STD_FILES.items()}
    return {k: f"decrement_tables/versions/{version}/{v}" for k, v in STD_FILES.items()}


def normalize_table(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """업로드된 테이블의 헤더를 표준 컬럼명으로 정규화하고 필수 컬럼을 검증한다."""
    renamed = df.rename(
        columns={c: COLUMN_ALIASES.get(str(c).strip(), str(c).strip()) for c in df.columns}
    )
    need = REQUIRED_COLS[kind]
    missing = [c for c in need if c not in renamed.columns]
    if missing:
        raise ValueError(
            f"{kind}: 필요한 컬럼이 없습니다 {missing}. "
            f"(업로드 파일 컬럼: {list(df.columns)}; 필요: {need})"
        )
    out = renamed[need].copy()
    # 숫자화 (문자 섞이면 오류)
    for c in need:
        out[c] = pd.to_numeric(out[c], errors="raise")
    return out


def _valid_name(name: str) -> bool:
    name = (name or "").strip()
    return bool(name) and not any(c in name for c in '\\/:*?"<>|')


def save_version(
    config_dir: Union[str, Path],
    name: str,
    tables: Dict[str, pd.DataFrame],
    description: str = "",
    created: Optional[str] = None,
) -> Path:
    """새 버전을 저장한다.

    tables: {종류: 정규화된 DataFrame}. 빠진 종류는 '기본' 버전에서 복사한다.
    반환: 저장된 버전 폴더 경로.
    """
    if not _valid_name(name):
        raise ValueError("버전 이름이 비었거나 사용할 수 없는 문자를 포함합니다.")
    name = name.strip()
    dest = versions_dir(config_dir) / name
    dest.mkdir(parents=True, exist_ok=True)
    base = base_dir(config_dir)

    for kind, fname in STD_FILES.items():
        if kind in tables and tables[kind] is not None:
            normalize_table(tables[kind], kind).to_csv(dest / fname, index=False)
        else:
            src = base / fname
            if src.exists():
                shutil.copy(src, dest / fname)

    meta = {"name": name, "description": description, "created": created or ""}
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


def delete_version(config_dir: Union[str, Path], name: str) -> bool:
    d = versions_dir(config_dir) / name
    if d.exists() and d.is_dir():
        shutil.rmtree(d)
        return True
    return False
