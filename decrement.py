"""다중탈퇴율 처리 — 탈퇴율 테이블 로딩·조회.

퇴직률(연령별 또는 근속별)과 성별·연령별 사망률 테이블을 CSV에서 로드하고
조회 인터페이스를 제공한다. 실제 다중탈퇴 확률 합성(재직잔존확률 등)은
프롬프트 2의 engine.py에서 이 테이블을 사용해 수행한다.

테이블 형식:
  retirement_rates_age.csv     : age, rate
  retirement_rates_service.csv : service, rate
  mortality.csv                : age, male_qx, female_qx
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple, Union

import numpy as np
import pandas as pd

from .config import Config, DecrementTableConfig
from .models import Gender


class DecrementTables:
    """퇴직률·사망률 테이블 컨테이너.

    조회는 정의역을 벗어나면 경계값(최소/최대 키)을 clamp 하여 반환한다.
    (계산 중 KeyError로 중단되지 않도록. 실무상 테이블 밖 연령은 경계율 적용.)

    성능: 조회용 dict를 한 번만 만들어 캐시한다(_get_map). 개인별 연도 루프에서
    벡터 조회(retirement_rates/mortality_rates)로 O(연수) 파이썬 루프만 돌린다.
    """

    def __init__(
        self,
        retirement_by_age: Optional[pd.DataFrame] = None,
        retirement_by_service: Optional[pd.DataFrame] = None,
        mortality: Optional[pd.DataFrame] = None,
    ) -> None:
        self._ret_age = retirement_by_age
        self._ret_service = retirement_by_service
        self._mortality = mortality
        # (id(df), key_col, val_col) -> (dict, lo, hi) 조회 캐시
        self._cache: Dict[Tuple[int, str, str], Tuple[dict, int, int]] = {}

    # -- 로딩 -----------------------------------------------------------------

    @classmethod
    def from_config(
        cls, config: Config, base_dir: Union[str, Path]
    ) -> "DecrementTables":
        """config의 테이블 경로(base_dir 상대)를 읽어 로드한다."""
        return cls.from_paths(config.decrement_tables, base_dir)

    @classmethod
    def from_paths(
        cls, table_cfg: DecrementTableConfig, base_dir: Union[str, Path]
    ) -> "DecrementTables":
        base = Path(base_dir)

        def _load(rel: Optional[str]) -> Optional[pd.DataFrame]:
            if not rel:
                return None
            p = base / rel
            if not p.exists():
                return None
            return pd.read_csv(p)

        return cls(
            retirement_by_age=_load(table_cfg.retirement_by_age),
            retirement_by_service=_load(table_cfg.retirement_by_service),
            mortality=_load(table_cfg.mortality),
        )

    # -- 조회 -----------------------------------------------------------------

    def _get_map(
        self, df: pd.DataFrame, key_col: str, val_col: str
    ) -> Tuple[dict, int, int]:
        """정수 키 -> 값 dict과 (최소, 최대) 키를 캐시하여 반환."""
        cache_key = (id(df), key_col, val_col)
        cached = self._cache.get(cache_key)
        if cached is None:
            keys = df[key_col].astype(int).to_numpy()
            vals = df[val_col].astype(float).to_numpy()
            d = {int(k): float(v) for k, v in zip(keys, vals)}
            cached = (d, int(keys.min()), int(keys.max()))
            self._cache[cache_key] = cached
        return cached

    def _lookup_one(self, df: pd.DataFrame, key_col: str, val_col: str, key: float) -> float:
        """정확 일치 우선, 정의역 밖은 clamp, 중간 결측은 이하 최대 키(계단식)."""
        d, lo, hi = self._get_map(df, key_col, val_col)
        k = int(round(key))
        if k < lo:
            k = lo
        elif k > hi:
            k = hi
        while k not in d and k > lo:
            k -= 1
        return d.get(k, 0.0)

    def _lookup_vec(
        self, df: pd.DataFrame, key_col: str, val_col: str, keys
    ) -> np.ndarray:
        """키 배열에 대한 벡터 조회 (개인별 연도 루프용)."""
        d, lo, hi = self._get_map(df, key_col, val_col)
        out = np.empty(len(keys), dtype=float)
        for i, key in enumerate(keys):
            k = int(round(float(key)))
            if k < lo:
                k = lo
            elif k > hi:
                k = hi
            while k not in d and k > lo:
                k -= 1
            out[i] = d.get(k, 0.0)
        return out

    # 스칼라 조회 (단건 확인용)
    def retirement_rate_by_age(self, age: float) -> float:
        if self._ret_age is None:
            raise ValueError("연령별 퇴직률 테이블이 로드되지 않았습니다.")
        return self._lookup_one(self._ret_age, "age", "rate", age)

    def retirement_rate_by_service(self, service: float) -> float:
        if self._ret_service is None:
            raise ValueError("근속별 퇴직률 테이블이 로드되지 않았습니다.")
        return self._lookup_one(self._ret_service, "service", "rate", service)

    def mortality_rate(self, age: float, gender: Gender) -> float:
        if self._mortality is None:
            raise ValueError("사망률 테이블이 로드되지 않았습니다.")
        col = "male_qx" if gender == Gender.M else "female_qx"
        return self._lookup_one(self._mortality, "age", col, age)

    # 벡터 조회 (엔진 내부 연도 루프용)
    def retirement_rates(self, keys, basis: str) -> np.ndarray:
        """basis('age'|'service') 기준 퇴직률 배열."""
        if basis == "age":
            if self._ret_age is None:
                raise ValueError("연령별 퇴직률 테이블이 로드되지 않았습니다.")
            return self._lookup_vec(self._ret_age, "age", "rate", keys)
        if self._ret_service is None:
            raise ValueError("근속별 퇴직률 테이블이 로드되지 않았습니다.")
        return self._lookup_vec(self._ret_service, "service", "rate", keys)

    def mortality_rates(self, ages, gender: Gender) -> np.ndarray:
        """성별·연령별 사망률 배열."""
        if self._mortality is None:
            raise ValueError("사망률 테이블이 로드되지 않았습니다.")
        col = "male_qx" if gender == Gender.M else "female_qx"
        return self._lookup_vec(self._mortality, "age", col, ages)
