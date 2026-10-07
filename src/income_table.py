"""도시근로자 가구원수별 가구당 월평균소득 기준표.

청약 제도는 1·2·3인 가구를 "3인 이하" 한 구간으로 묶어 같은 금액을 적용한다.
자녀가 없는 예비신혼부부(2인)도 3인 이하 구간을 쓴다.

통계청이 전년도 수치를 발표하면(보통 3월) 아래 표를 갱신해야 한다.
출처: 서울주택도시공사 공고 기준표 https://www.i-sh.co.kr/app/lay2/S48T1587C589/contents.do
"""

# 2025년도 적용 기준 (2024년 도시근로자 가구당 월평균소득, 100% 기준액, 원)
MONTHLY_100 = {
    3: 8_168_429,  # 3인 이하
    4: 8_802_202,
    5: 9_326_985,
    6: 10_181_384,
    7: 11_035_783,
    8: 11_890_182,
}

MIN_BRACKET = 3  # 1~3인은 모두 3인 기준


def baseline(household_size: int) -> int:
    """가구원수에 해당하는 100% 기준 월소득(원)."""
    size = max(int(household_size or MIN_BRACKET), MIN_BRACKET)
    if size in MONTHLY_100:
        return MONTHLY_100[size]
    # 8인 초과는 1인당 증가분을 그대로 더한다 (제도상 관행)
    step = MONTHLY_100[8] - MONTHLY_100[7]
    return MONTHLY_100[8] + step * (size - 8)


def monthly_income(*annual_incomes) -> int:
    """연소득들을 합산해 월평균 소득으로 환산한다."""
    total = sum(int(a or 0) for a in annual_incomes)
    return round(total / 12)


def person_monthly(person: dict) -> int:
    """한 사람의 월 소득.

    청약 소득 심사는 건강보험 보수월액을 기준으로 삼는 경우가 많다. 그 값이
    있으면 그대로 쓰고, 없을 때만 연소득을 12로 나눠 추정한다. 추정값은
    상여·수당 반영 방식이 달라 실제 심사액과 어긋날 수 있다.
    """
    monthly = person.get("monthly_income")
    if monthly:
        return int(monthly)
    annual = person.get("annual_income")
    return round(int(annual) / 12) if annual else 0


def household_monthly(*people: dict) -> int:
    """부부 합산 월 소득."""
    return sum(person_monthly(p or {}) for p in people)


def household_income(profile: dict) -> int:
    """가구 합산 월 소득. household.monthly_income 이 있으면 그 값(폼에서 구간으로 받은 값)을 쓴다.

    베타는 지인이 정확한 연봉 대신 구간을 고르므로, 사람별 값을 더하면 구간 폭이
    겹쳐 합산이 부정확해진다. 그래서 가구 합산 구간을 따로 받아 여기에 둔다.
    """
    explicit = (profile.get("household") or {}).get("monthly_income")
    if explicit is not None:
        return int(explicit)
    return household_monthly(profile.get("applicant") or {}, profile.get("spouse") or {})


def income_ratio(monthly: int, household_size: int) -> float:
    """월평균 소득이 기준의 몇 %인지 반환한다."""
    return monthly / baseline(household_size) * 100.0


# 특별공급 유형별 소득 구간 (단위 %).
# 값은 (외벌이 상한, 맞벌이 상한). None 이면 소득 요건 없음.
#
# 주의: 공고마다 배분 비율과 상한이 다를 수 있다. 아래는 현행 일반적인 기준이며
# 최종 확인은 항상 모집공고문이다.
BRACKETS = {
    "신혼부부특공": {
        "우선공급": (100, 120),
        "일반공급": (140, 160),
        "추첨": (None, 200),
    },
    "생애최초특공": {
        "우선공급": (100, 120),
        "일반공급": (130, 140),
        "추첨": (130, 200),
    },
    "신생아특공": {
        "우선공급": (130, 130),
        "일반공급": (160, 160),
        "추첨": (None, None),
    },
}

# 추첨 물량에 적용되는 자산 요건 (원)
ASSET_LIMIT_REAL_ESTATE = 331_000_000  # 부동산 가액
