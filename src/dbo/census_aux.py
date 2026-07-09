"""보조 명부(퇴직자·전출입) 사번 추출 — 교차검증용.

퇴직자·전출입 명부는 표준 Employee 스키마가 아니므로 load_census로 못 읽는다.
여기서는 사번(과 전출입 사유) 컬럼만 견고하게 뽑아 재직자명부와 대사한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple, Union

from .census import _emp_id_to_str, _norm_header, _read_dataframe

_ID_ALIASES = ["사번", "사원번호", "직원번호", "사원코드", "emp_id"]
_REASON_ALIASES = ["사유", "사유코드", "전출입사유"]


def _find_col(df, aliases) -> Optional[str]:
    norm = {}
    for c in df.columns:
        norm.setdefault(_norm_header(c), c)
    for a in aliases:
        col = norm.get(_norm_header(a))
        if col is not None:
            return col
    return None


def read_ids(path: Union[str, Path]) -> List[str]:
    """명부 파일에서 사번 목록을 추출(정규화). 컬럼을 못 찾으면 빈 리스트."""
    try:
        df = _read_dataframe(path)
    except Exception:  # noqa: BLE001
        return []
    col = _find_col(df, _ID_ALIASES)
    if col is None:
        return []
    out = []
    for v in df[col]:
        eid = _emp_id_to_str(v)
        if eid:
            out.append(eid)
    return out


def read_transfer(path: Union[str, Path]) -> List[Tuple[str, Optional[int]]]:
    """전출입명부에서 (사번, 사유코드) 목록. 사유: 1전입 2전출 3사업결합 4사업처분 5기타장기."""
    try:
        df = _read_dataframe(path)
    except Exception:  # noqa: BLE001
        return []
    idc = _find_col(df, _ID_ALIASES)
    if idc is None:
        return []
    rc = _find_col(df, _REASON_ALIASES)
    out: List[Tuple[str, Optional[int]]] = []
    for _, row in df.iterrows():
        eid = _emp_id_to_str(row[idc])
        if not eid:
            continue
        reason = None
        if rc is not None:
            try:
                reason = int(float(row[rc]))
            except (TypeError, ValueError):
                reason = None
        out.append((eid, reason))
    return out
