"""샘플 명부 생성 스크립트 — 더미 데이터 500명.

다양한 연령·근속·중간정산자·임원·제도구분을 혼합한 더미 명부를 생성한다.
개인정보가 아닌 합성 데이터이며, 한글 컬럼명으로 저장해 컬럼 매핑 기능을
함께 시험할 수 있다.

사용:
  python scripts/generate_sample_census.py --out data/sample_census.xlsx --n 500 --seed 42
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
from pathlib import Path

import pandas as pd

VAL_YEAR = 2025  # 산출기준일 2025-12-31 가정


def generate(n: int = 500, seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed)
    rows = []
    for k in range(n):
        emp_id = f"{10000 + k}"
        age = rng.randint(24, 63)                      # 일부 고령(경고 유발 가능)
        max_service = max(1, min(age - 20, 35))
        service = rng.randint(1, max_service)
        birth = dt.date(VAL_YEAR - age, rng.randint(1, 12), rng.randint(1, 28))
        hire = dt.date(VAL_YEAR - service, rng.randint(1, 12), rng.randint(1, 28))

        gender = rng.choice(["남", "여"])
        emp_class = rng.choices(["정규직", "임원", "계약직"], weights=[75, 10, 15])[0]
        plan = rng.choices([1, 2, 3], weights=[80, 12, 8])[0]

        base_salary = rng.randint(200, 800) * 10000    # 200만~800만
        accrual = int(base_salary * service * rng.uniform(0.9, 1.1))
        next_accrual = int(base_salary * (service + 1) * rng.uniform(0.9, 1.1))  # 차년도
        ifrs = rng.choices(["Y", "N"], weights=[85, 15])[0]

        # 약 15%는 중간정산자
        interim_date = None
        interim_amount = None
        if rng.random() < 0.15 and service >= 2:
            interim_service = rng.randint(1, service)
            interim_date = dt.date(VAL_YEAR - interim_service, rng.randint(1, 12), rng.randint(1, 28))
            # 중간정산기준일이 입사일보다 앞서지 않도록 보정
            if interim_date < hire:
                interim_date = hire
            interim_amount = int(base_salary * interim_service * rng.uniform(0.9, 1.1))

        multiplier = 2.0 if emp_class == "임원" and rng.random() < 0.5 else 1.0

        rows.append(
            {
                "사번": emp_id,
                "생년월일": birth,
                "성별": gender,
                "입사일": hire,
                "기준급여": base_salary,
                "당년도추계액": accrual,
                "차년도추계액": next_accrual,
                "IFRS가입": ifrs,
                "종업원구분": emp_class,
                "중간정산기준일": interim_date,
                "중간정산액": interim_amount,
                "제도구분": plan,
                "적용배수": multiplier,
            }
        )
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="더미 명부 생성 (500명)")
    p.add_argument("--out", type=Path, default=Path("data/sample_census.xlsx"))
    p.add_argument("--n", type=int, default=500)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    df = generate(args.n, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.suffix.lower() == ".csv":
        df.to_csv(args.out, index=False, encoding="utf-8-sig")
    else:
        df.to_excel(args.out, index=False)
    print(f"샘플 명부 {len(df)}명 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
