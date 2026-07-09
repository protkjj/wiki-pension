"""감사 대응 질의응답 생성 — 정제된 답변 + 근거 자동 인용.

감사인이 흔히 묻는 질문에 대해, 실제 계산에 사용된 가정·결과 수치를 인용한
정제된 표준 답변을 자동 생성한다. 외부 AI 없이 실제 산출값을 근거로 제시한다.

반환: [{"q": 질문, "a": 답변, "basis": 근거}, ...]
"""

from __future__ import annotations

from typing import List, Optional

from .config import Config
from .decrement import DecrementTables
from .engine import CensusResult
from .outputs import build_maturity, build_sensitivity


def _won(v) -> str:
    return f"{(v or 0):,.0f}원"


def _pct(v) -> str:
    return f"{v*100:.2f}%"


def build_audit_qa(
    records,
    config: Config,
    tables: DecrementTables,
    result: CensusResult,
    plan_info: Optional[dict] = None,
    prior: Optional[dict] = None,
) -> List[dict]:
    """감사 대응 표준 질의응답 목록을 생성한다."""
    sens = build_sensitivity(records, config, tables)
    duration, _mat = build_maturity(records, config, tables)

    def s_row(name):
        r = sens[sens["시나리오"] == name]
        if len(r):
            return float(r["총 DBO"].iloc[0]), float(r["변화율(%)"].iloc[0])
        return None, None

    d_up, d_up_pct = s_row("할인율 +0.5%p")
    d_dn, d_dn_pct = s_row("할인율 -0.5%p")
    s_up, s_up_pct = s_row("임금상승률 +0.5%p")

    disc = config.discount_rate.flat
    sal = config.salary_increase_rate.flat
    qa: List[dict] = []

    qa.append({
        "q": "확정급여채무(DBO)는 어떤 방법으로 산정하였습니까?",
        "a": ("한국채택국제회계기준 제1019호(종업원급여)에 따른 예측단위적립방식(PUC, "
              "Projected Unit Credit)으로 산정하였습니다. 각 종업원이 정년까지 근무함으로써 "
              "발생하는 미래 퇴직급여를, 퇴직·사망 등 탈퇴율과 할인율을 반영하여 기대현재가치로 "
              "평가하고, 산출기준일 현재까지의 근무비율만큼 배분하여 채무를 인식하였습니다."),
        "basis": (f"산출기준일 {config.valuation_date}, 총 확정급여채무 {_won(result.total_dbo)}, "
                  f"당기근무원가 {_won(result.total_csc)}, 계산대상 {len(result.results)}명. "
                  f"근거: K-IFRS 제1019호 문단 67–68(PUC 적용)."),
    })
    qa.append({
        "q": "적용한 할인율과 그 근거는 무엇입니까?",
        "a": (f"할인율은 연 {_pct(disc)}를 적용하였습니다. K-IFRS 제1019호에 따라 보고기간말 "
              f"우량회사채의 시장수익률을 참조하여 결정하였습니다. (구체 근거 지수·만기는 "
              f"평가 시점 채권시장 자료에 따름)"),
        "basis": (f"적용 할인율 {_pct(disc)}. 민감도: 할인율 +0.5%p 시 총 DBO {_won(d_up)}"
                  f"({d_up_pct:+.2f}%), -0.5%p 시 {_won(d_dn)}({d_dn_pct:+.2f}%). "
                  f"근거: K-IFRS 제1019호 문단 83."),
    })
    qa.append({
        "q": "임금상승률 가정은 어떻게 설정하였습니까?",
        "a": (f"임금상승률은 연 {_pct(sal)}를 적용하였으며, 과거 임금인상 추세와 승급·물가 "
              f"전망을 고려하여 설정하였습니다."),
        "basis": (f"적용 임금상승률 {_pct(sal)}. 민감도: 임금상승률 +0.5%p 시 총 DBO "
                  f"{_won(s_up)}({s_up_pct:+.2f}%). 근거: K-IFRS 제1019호 문단 87–90."),
    })
    qa.append({
        "q": "퇴직률·사망률 등 인구통계적 가정의 출처는 무엇입니까?",
        "a": (f"퇴직률은 {config.retirement_rate_basis}(연령/근속) 기준 경험률 테이블을, "
              f"사망률은 성별·연령별 경험생명표를 적용하였습니다. 정년은 "
              f"{config.retirement_age.default}세(종업원구분별 별도 적용 가능)로 가정하였습니다."),
        "basis": (f"퇴직률 적용기준: {config.retirement_rate_basis}, 정년(기본) "
                  f"{config.retirement_age.default}세. 근거: K-IFRS 제1019호 문단 76."),
    })
    qa.append({
        "q": "가정 변동에 따른 확정급여채무의 민감도는 어떻게 됩니까?",
        "a": ("주요 가정 ±0.5%p 변동에 따른 확정급여채무 변동은 다음과 같습니다. 할인율 상승은 "
              "채무를 감소시키고, 임금상승률 상승은 채무를 증가시킵니다."),
        "basis": (f"할인율 +0.5%p: {d_up_pct:+.2f}%, -0.5%p: {d_dn_pct:+.2f}%; "
                  f"임금상승률 +0.5%p: {s_up_pct:+.2f}%. 근거: K-IFRS 제1019호 문단 145(민감도 공시)."),
    })
    qa.append({
        "q": "확정급여채무의 가중평균만기(듀레이션)는 얼마입니까?",
        "a": (f"확정급여채무의 가중평균만기는 약 {duration:.1f}년입니다. 이는 미래 예상 급여지급의 "
              f"현재가치를 가중치로 한 평균 지급시점을 의미합니다."),
        "basis": (f"가중평균만기 {duration:.1f}년. 근거: K-IFRS 제1019호 문단 147(만기 프로파일)."),
    })

    if plan_info:
        qa.append({
            "q": "대상 회사의 퇴직급여제도 및 산정기준은 무엇입니까?",
            "a": (f"제도유형은 {plan_info.get('plan_type') or '-'}, 급여산정기준은 "
                  f"{plan_info.get('salary_basis') or '-'}, 퇴직금 규정은 "
                  f"{plan_info.get('benefit_rule') or '-'}입니다. "
                  f"사외적립은 {plan_info.get('external_funding') or '없음'}"
                  + (f"(적립비율 {plan_info.get('funding_ratio')}%)" if plan_info.get('funding_ratio') else "")
                  + "입니다."),
            "basis": (f"제도설정일 {plan_info.get('established_date') or '-'}, 정년 "
                      f"{plan_info.get('retirement_age') or '-'}세. 출처: 회사 제출 제도정보."),
        })

    if prior and prior.get("prior_dbo"):
        diff = result.total_dbo - (prior.get("prior_dbo") or 0)
        qa.append({
            "q": "전기 대비 확정급여채무의 증감과 그 원인은 무엇입니까?",
            "a": (f"전기말 확정급여채무 {_won(prior.get('prior_dbo'))} 대비 당기 "
                  f"{_won(result.total_dbo)}로 {_won(diff)} 변동하였습니다. 주요 원인은 근무원가·"
                  f"이자원가·가정 변경 및 경험조정 등입니다."),
            "basis": (f"전기 계리법인: {prior.get('prior_firm') or '-'}, 전기 산출기준일 "
                      f"{prior.get('prior_valuation_date') or '-'}. 근거: K-IFRS 제1019호 문단 141(변동조정)."),
        })

    return qa
