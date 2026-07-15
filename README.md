# DBO 계산 엔진 (dbo-engine)

K-IFRS 제1019호(종업원급여)에 따른 확정급여채무(**DBO**, Defined Benefit Obligation)를
예측단위적립방식(**PUC**, Projected Unit Credit)으로 계산하는 엔진입니다.

- 명부(census) 로딩·검증 → PUC 계산 → 엑셀 산출물·실행로그 생성
- 기존 엑셀 계산과의 개인별 **대사(對査)** 및 convention 자동 탐색
- 모든 계산 가정은 YAML로 제어(설정 기반), 결과는 **바이트 단위 재현 가능**

> ⚠️ `CLAUDE.md`의 "도메인 지식: K-IFRS 1019 PUC 방식" 수식 섹션은 **초안**입니다.
> timing convention과 CSC 산출 방식은 회사 실무에 따라 다를 수 있으므로, 실제 엑셀
> 결과와 [대사](#대사-도구)하면서 확정·튜닝하는 것을 전제로 설계되었습니다.

---

## 설치

Python 3.11+ 필요.

```bash
cd dbo-engine
pip install -e .            # 또는: pip install -e ".[dev]" (pytest 포함)
```

의존성: pydantic v2, pandas, numpy, pyyaml, openpyxl.

---

## 빠른 시작

```bash
# 1) 더미 명부 500명 생성 (한글 컬럼)
python scripts/generate_sample_census.py --out data/sample_census.xlsx --n 500

# 2) 명부 검증만 수행
dbo validate --census data/sample_census.xlsx \
             --config config/assumptions_sample.yaml \
             --map config/column_map_sample.yaml

# 3) 전체 계산 + 산출물 생성 (특정 사번 상세 덤프 포함)
dbo run --census data/sample_census.xlsx \
        --config config/assumptions_sample.yaml \
        --map config/column_map_sample.yaml \
        --out data/results/ --debug-emp 10000

# 4) 기존 엑셀과 개인별 대사
dbo reconcile --engine data/results/dbo_results.xlsx \
              --excel data/기존계산.xlsx \
              --map config/excel_map_sample.yaml --out data/results/reconcile.xlsx

# 5) 기존 엑셀과 가장 일치하는 convention 조합 탐색
dbo reconcile-sweep --census data/sample_census.xlsx \
                    --config config/assumptions_sample.yaml \
                    --census-map config/column_map_sample.yaml \
                    --excel data/기존계산.xlsx --map config/excel_map_sample.yaml \
                    --grid config/sweep_sample.yaml
```

> `dbo` CLI는 `pip install -e .` 후 사용 가능. 미설치 시 `PYTHONPATH=src python -m dbo.cli ...`.

---

## 디렉토리 구조

```
dbo-engine/
  src/dbo/
    models.py        # 종업원 표준 스키마 (pydantic)
    config.py        # 계산 설정 + YAML 로더
    census.py        # 명부 로딩·컬럼매핑·검증
    decrement.py     # 퇴직률·사망률 테이블 로딩·조회
    engine.py        # PUC 계산 코어
    outputs.py       # 엑셀 산출물·실행로그
    reconcile.py     # 엑셀 대사 도구
    cli.py           # CLI 진입점 (dbo)
  config/
    assumptions_sample.yaml     # 계산 설정 샘플
    column_map_sample.yaml      # 명부 한글 컬럼 매핑 샘플
    excel_map_sample.yaml       # 대사용 엑셀 컬럼 매핑 샘플
    sweep_sample.yaml           # convention 탐색 그리드 샘플
    decrement_tables/*.csv      # 퇴직률·사망률 더미 테이블
  scripts/generate_sample_census.py   # 더미 명부 생성기
  tests/                              # pytest (손계산·검증·통합·재현성·대사)
  data/               # 실데이터·산출물 (git 제외)
```

---

## 명부 표준 스키마

| 필드 | 설명 | 필수 |
|---|---|:---:|
| `emp_id` | 사번 (문자열, **성명은 받지 않음**) | ✅ |
| `birth_date` | 생년월일 | ✅ |
| `gender` | 성별 `M`/`F` | ✅ |
| `hire_date` | 입사일 | ✅ |
| `base_salary` | 기준급여(월평균임금, 원) | ✅ |
| `current_year_accrual` | 당년도추계액(기준일 일시 지급 시 퇴직금) | ✅ |
| `next_year_accrual` | 차년도추계액(차기말 예상 퇴직금) | |
| `ifrs_enrolled` | IFRS 가입 여부 `Y`/`N` | |
| `emp_class` | 종업원구분 `EXECUTIVE`/`REGULAR`/`CONTRACT` | ✅ |
| `interim_settlement_date` | 중간정산기준일 | |
| `interim_settlement_amount` | 중간정산액 | |
| `plan_type` | 제도구분 `1`=DB정상 / `2`=간편법 / `3`=제외 (기본 1) | |
| `multiplier` | 적용배수 (기본 1.0) | |

**한글 컬럼 매핑**: 고객마다 양식이 달라 `config/column_map_sample.yaml`에서 컬럼명
별칭과 값(남/여→M/F, 임원→EXECUTIVE 등)을 지정합니다.

```yaml
columns:
  emp_id: [사번, 사원번호]
  base_salary: [기준급여, 월평균임금]
  ...
values:
  gender: {남: M, 여: F}
  emp_class: {임원: EXECUTIVE, 정규직: REGULAR, 계약직: CONTRACT}
```

**검증 규칙** (오류=계산중단 / 경고=플래그 후 진행):

- 오류: 입사일>기준일, 만 15세 미만, 생년월일≥입사일, 중간정산기준일<입사일 또는 >기준일, 기준급여≤0
- 경고: 만 80세 초과, 중간정산 기준일·금액 짝 누락, 당년도추계액<0, 적용배수≤0

---

## 설정(config) 항목

`config/assumptions_sample.yaml`. 스칼라 축약(`discount_rate: 0.045`)과 명시 표기 모두 지원.

| 항목 | 설명 | 기본값 |
|---|---|---|
| `valuation_date` | 산출기준일 | (필수) |
| `discount_rate` | 할인율 (단일값, 커브 확장 가능 구조) | (필수) |
| `salary_increase_rate` | 임금상승률 (단일값, 연령/근속 테이블 확장 가능) | (필수) |
| `retirement_age` | 정년 (`default` + 종업원구분별 `by_class`) | 60 |
| `decrement_timing` | 탈퇴 발생 시점 `end_of_year`/`mid_year` | end_of_year |
| `salary_increase_timing` | 임금상승 반영 시점 `start_of_year`/`mid_year`/`end_of_year` | start_of_year |
| `discount_timing` | 탈퇴시점까지 할인기간 산정 `end_of_year`/`mid_year` | end_of_year |
| `service_day_count` | 근속 일할 `act/365`/`act/365.25`/`months` | act/365 |
| `retirement_rate_basis` | 퇴직률 적용 기준 `age`/`service` | age |
| `csc_method` | 당기근무원가 산출 `one_year_slice`/`attained_minus_prior` | one_year_slice |
| `rounding` | 최종 반올림 단위(원) | 1 |
| `decrement_tables` | 퇴직률·사망률 CSV 경로 | (샘플 제공) |

### convention 각 선택지가 결과에 미치는 영향

계산 코어는 개인별로 미래 연도 t(기준일 익일~정년)마다 다음을 산정합니다. 표기:
x₀=도달연령, s₀=도달근속, S(t)=탈퇴시점 총근속, v=1/(1+할인율).

**개인 DBO = Σₜ 예상급여(t) × s₀ × 배수 × 탈퇴확률(t) × 할인계수(t)**

| convention | 선택지 | DBO에 미치는 영향 |
|---|---|---|
| `service_day_count` | act/365 vs act/365.25 vs months | **s₀(현재근속)를 직접 스케일** → DBO에 비례적으로 영향. **가장 큰 레버.** |
| `salary_increase_timing` | start/mid/end_of_year | 예상급여 지수 e = t−1 / t−0.5 / t. 값이 클수록 급여·DBO ↑. |
| `discount_timing` | end vs mid_year | 할인기간 p = t vs t−0.5. mid_year면 할인기간이 짧아져 DBO ↑. |
| `retirement_rate_basis` | age vs service | 퇴직률을 도달연령/도달근속 중 무엇으로 조회할지 → 탈퇴확률 분포 변화. |
| `csc_method` | one_year_slice vs attained_minus_prior | CSC 산출 방식. 급여가 근속에 선형인 기본 공식에선 **두 방식 수치 동일**. |
| `decrement_timing` | end vs mid_year | **DBO 무영향.** 탈퇴시점 총근속 S(t)가 재직비율(s₀/S(t))과 상쇄되고, 할인기간은 `discount_timing`이 별도 제어. 상세표의 도달연령·도달근속 표시에만 영향. |
| `discount_rate` | — | 높을수록 DBO ↓ (미래급여 현가↓). 민감도 시트에 ±0.5%p 제공. |
| `salary_increase_rate` | — | 높을수록 DBO ↑ (예상급여↑). 민감도 시트에 ±0.5%p 제공. |

> **대사 시 실제로 조정할 레버**는 `service_day_count`, `salary_increase_timing`,
> `discount_timing`(및 CSC는 `csc_method`)입니다. `decrement_timing`은 DBO를 움직이지 않습니다.

**제도구분 처리**: `1`=PUC 정상계산, `2`=당년도추계액을 그대로 부채로 계상,
`3`=결과에서 제외하되 별도 목록 출력. **중간정산자**는 중간정산기준일부터 근속을 기산합니다.

### 탈퇴율 테이블 형식 (CSV)

```
retirement_rates_age.csv       age,rate
retirement_rates_service.csv   service,rate
mortality.csv                  age,male_qx,female_qx
```

`config/decrement_tables/`의 값은 **더미**이며 실제 경험률로 교체해야 합니다.
테이블 정의역 밖 연령/근속은 경계값으로 clamp됩니다.

---

## 산출물

`dbo run` 결과 (`--out` 디렉토리):

- **`dbo_results.xlsx`**
  - `개인별산출표`: 사번·인적정보·근속·급여·추계액·DBO·CSC·제도구분·검증플래그
  - `요약`: 총 DBO/CSC, 인원, 종업원구분별/제도구분별 소계, 적용 가정 전체
  - `민감도분석`: 할인율 ±0.5%p, 임금상승률 ±0.5%p DBO 재계산
  - `만기분석`: 가중평균만기(듀레이션) + 향후 10년 개별 + 이후 5년 구간별 기대급여 현금흐름
  - `제외목록`: 제도구분 3 사번
- **`run_log.json`**: 입력 파일 SHA-256, config 전체 스냅샷, 엔진 버전, 실행시각, 총 결과값
- **`detail_<사번>.csv`** (`--debug-emp` 지정 시): 해당 사번의 연도별 상세 기여분

---

## 플랫폼 (3역할 워크플로우, MVP)

기업 담당자·계리인·관리자가 협업하는 다중 사용자 웹앱(프로토타입).

```bash
pip install -e ".[app]"
streamlit run app/platform_app.py      # Windows는 run_platform.bat 더블클릭
```

- **기업 담당자(client)**: 명부 업로드 → 자동 검증(문제 행 즉시 표시) → 수정·제출
- **계리인(actuary)**: 제출 건 계산 → 산출물·보고서 확정
- **관리자(admin)**: 전체 기업 진행현황·결과·이력 대시보드
- 데모 계정: `admin/admin123`, `actuary/act123`, `clientA/ca123`, `clientB/cb123`
- 데이터는 `data/platform/`(SQLite + 파일, git 제외)에 저장

> ⚠️ **개인정보·보안**: 외부 기업 고객에게 실제 명부를 올리게 하려면 **배포 위치·
> 전송/저장 암호화·회사별 접근통제·개인정보보호법 준수**를 먼저 확정해야 합니다.
> 현재는 기능 검증용 프로토타입이며, 실데이터·외부 오픈 전 사내 IT/보안/법무 협의 필요.

---

## 웹 앱 (단일 사용자, Streamlit)

명부 업로드 → 가정 입력 → 계산·결과 조회·다운로드 → 엑셀 대사를 브라우저에서
수행하는 로컬 실행용 화면입니다.

```bash
pip install -e ".[app]"                # streamlit 포함 설치
streamlit run app/streamlit_app.py     # 브라우저에서 http://localhost:8501
```

- **① 명부·검증**: 파일 업로드(한글 컬럼 매핑 지원), 오류/경고 리포트
- **② 계산 결과**: 총 DBO/CSC·구분별 소계·민감도·만기 차트, 개인별 표, 엑셀/로그 다운로드
- **③ 엑셀 대사**: 기존 엑셀 업로드 → 개인별 비교 또는 convention 탐색(sweep)

> ⚠️ **개인정보**: 실제 명부는 이 앱을 **로컬/사내망**에서 실행할 때만 업로드하세요.
> 공개 클라우드에 배포된 인스턴스에는 실데이터를 올리지 않습니다.

---

## 대사 도구 (CLI)

기존 엑셀 계산과 엔진 결과를 맞춰보고, 어떤 convention 조합이 기존 값과 일치하는지 탐색합니다.

- **`dbo reconcile`**: 사번 조인 → 개인별 차이(절대·비율), 총액차이, 허용오차 이내 비율,
  차이 상위 20명, 편측 사번 목록. 허용오차 옵션 `--abs-tol`(기본 1원) `--rel-tol`(기본 0.01%).
  `--track <사번> --census --config`로 개인 연도별 상세를 덤프해 추적.
- **`dbo reconcile-sweep`**: `config/sweep_sample.yaml`의 그리드(각 convention 후보)를 전부
  계산해 조합별 총액차이·일치율을 표로 출력하고, **가장 일치율 높은 조합을 추천**.

엑셀/엔진 결과 파일의 사번·DBO 컬럼 위치는 `config/excel_map_sample.yaml`로 지정합니다.

---

## 절대 원칙 (준수 현황)

1. **재현성** — 동일 입력·config → 산출물 **바이트 동일**(xlsx zip 타임스탬프 고정,
   실행시각은 로그 메타데이터로만). `tests/test_reproducibility.py`로 고정.
2. **투명성·감사가능성** — 모든 DBO/CSC는 개인별 연도별 상세 기여분의 합. 임의 사번
   상세를 CSV로 덤프 가능(`--debug-emp`).
3. **개인정보 보호** — 성명 등 스키마 밖 필드는 거부(`extra=forbid`), 사번만 사용.
   실데이터 `data/`는 git 제외.
4. **설정 기반** — 모든 가정은 YAML로 제어, 코드에 율(rate) 하드코딩 없음, 기본값 문서화.
5. **검증 우선** — 핵심 수식은 손계산 검증 테스트 보유, 명부는 계산 전 오류/경고 구분 검증.

---

## 테스트

```bash
pytest            # 전체
pytest tests/test_engine.py         # 손계산 단계별 검증
pytest tests/test_reproducibility.py
```

- `test_models` / `test_config` / `test_census`: 스키마·설정·검증 규칙
- `test_engine`: 기준선 → 할인 → 임금상승 → 퇴직률 → 사망률 단계별 손계산(주석에 계산과정)
- `test_integration`: 500명 전체 파이프라인 + CLI 스모크
- `test_reconcile`: 개인별 비교·convention 탐색 추천
- `test_reproducibility`: 2회 실행 결과 파일 바이트 동일

---

## PUC 수식 요약 (기본 convention)

미래 연도 t=1..N (N = round(정년 − 도달연령))에 대해:

```
예상급여   sal(t) = base_salary × (1+g)^e            # e: salary_increase_timing
탈퇴시점총근속 S(t) = s₀ + (t 또는 t−0.5)             # decrement_timing
재직비율        = s₀ / S(t)
할인계수 disc(t) = (1+i)^(−p)                         # p: discount_timing
다중탈퇴: 연내 사망 먼저 → 생존자 중 퇴직/정년, Σ 탈퇴확률 = 1
DBO기여분(t) = sal(t) × s₀ × 배수 × 탈퇴확률(t) × disc(t)
개인 DBO = Σₜ DBO기여분(t)
```

자세한 유도와 가정은 `CLAUDE.md`를 참고하세요.
