"""청약 자격 판정.

공고 하나에 대해 신청 가능한 경로(특별공급 유형, 일반공급, 무순위)를 각각
판정하고 근거 문장을 남긴다. 제도가 복잡하고 공고마다 예외가 있으므로 결과는
어디까지나 참고용이며, 최종 확인은 모집공고문이라는 전제로 쓴다.

판정 상태는 세 가지다.
  가능   — 요건을 충족한다고 볼 근거가 있다
  조건부 — 정보가 없거나(설정 미입력) 공고문 확인이 필요하다
  불가   — 명확히 요건에 미달한다
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import income_table
from .config import months_between, parse_date

POSSIBLE = "가능"
CONDITIONAL = "조건부"
IMPOSSIBLE = "불가"

# 청약 예치금 기준 (신청자 거주지 기준, 원)
DEPOSIT_BY_REGION = {
    "서울": {85: 3_000_000, 102: 6_000_000, 135: 10_000_000, 999: 15_000_000},
    "부산": {85: 3_000_000, 102: 6_000_000, 135: 10_000_000, 999: 15_000_000},
    "광역시": {85: 2_500_000, 102: 4_000_000, 135: 7_000_000, 999: 10_000_000},
    "기타": {85: 2_000_000, 102: 3_000_000, 135: 4_000_000, 999: 5_000_000},
}


@dataclass
class Route:
    """신청 경로 하나에 대한 판정 결과."""

    name: str
    status: str
    reason: str


@dataclass
class Verdict:
    """routes 는 신청 경로, checks 는 모든 경로에 공통으로 걸리는 전제 조건."""

    routes: list[Route] = field(default_factory=list)
    checks: list[Route] = field(default_factory=list)
    # 청약통장 순위. 메시지에서 따로 한 줄을 차지하므로 별도로 들고 있는다.
    account: Route | None = None
    # 부부는 각자 통장으로 따로 신청할 수 있어 사람별 순위를 함께 담는다.
    # [(이름, 판정), ...]
    accounts: list = field(default_factory=list)
    # 신혼부부 자격을 어느 쪽으로 봤는지 (예비 신혼부부 / 기혼)
    marriage_basis: str = ""
    gapjeom: int = 0
    gapjeom_detail: str = ""
    marriage_advice: str = ""
    income_ratio: float = 0.0

    @property
    def possible(self) -> list[Route]:
        return [r for r in self.routes if r.status == POSSIBLE]

    @property
    def conditional(self) -> list[Route]:
        return [r for r in self.routes if r.status == CONDITIONAL]

    @property
    def blocked(self) -> bool:
        """전제 조건에서 명확히 탈락한 경우."""
        return any(c.status == IMPOSSIBLE for c in self.checks)

    @property
    def eligible(self) -> bool:
        if self.blocked:
            return False
        return bool(self.possible or self.conditional)


# --------------------------------------------------------------------------
# 가점 계산
# --------------------------------------------------------------------------


def account_months(profile: dict, today: date, person_key: str = "applicant") -> int | None:
    opened = parse_date((profile.get(person_key) or {}).get("account", {}).get("opened_date"))
    if not opened:
        return None
    return months_between(opened, today)


def account_score(months: int | None) -> int:
    """청약통장 가입기간 가점 (최대 17점)."""
    if months is None:
        return 0
    if months < 6:
        return 1
    if months < 12:
        return 2
    years = months // 12
    return min(years + 2, 17)


def homeless_score(years: float) -> int:
    """무주택기간 가점 (최대 32점). 만 30세 또는 혼인신고일 중 빠른 날부터 기산."""
    return min(int(max(years, 0)) * 2 + 2, 32)


def dependents_score(count: int) -> int:
    """부양가족 가점 (최대 35점)."""
    return min((count + 1) * 5, 35)


def homeless_years(profile: dict, today: date, married: bool) -> float:
    """무주택 기간(년). 만 30세 전이라도 혼인신고를 하면 그날부터 기산된다."""
    applicant = profile.get("applicant") or {}
    household = profile.get("household") or {}
    since = parse_date(applicant.get("homeless_since"))
    if since:
        return months_between(since, today) / 12

    marriage = parse_date(household.get("marriage_date"))
    if married and marriage:
        return months_between(marriage, today) / 12

    birth = parse_date(applicant.get("birth_date"))
    age = applicant.get("age")
    if birth:
        age_years = months_between(birth, today) / 12
    elif age is not None:
        age_years = float(age)
    else:
        return 0.0
    return max(age_years - 30, 0.0)


def compute_gapjeom(profile: dict, today: date, married: bool) -> tuple[int, str]:
    household = profile.get("household") or {}
    months = account_months(profile, today)
    acc = account_score(months)

    hl_years = homeless_years(profile, today, married)
    hl = homeless_score(hl_years)

    # 부양가족: 배우자(혼인신고 후) + 자녀 + 3년 이상 같은 등본의 직계존속(부모·조부모)
    dependents = ((1 if married else 0) + int(household.get("children") or 0)
                  + int(household.get("parents_dependents") or 0))
    dep = dependents_score(dependents)

    total = acc + hl + dep
    months_text = f"{months // 12}년 {months % 12}개월" if months is not None else "미입력"
    detail = (
        f"통장 {acc}점({months_text}) + 무주택 {hl}점({hl_years:.0f}년) "
        f"+ 부양가족 {dep}점({dependents}명) = {total}점"
    )
    return total, detail


# --------------------------------------------------------------------------
# 개별 요건 판정
# --------------------------------------------------------------------------


def deposit_requirement(region: str, area: float) -> int:
    table = DEPOSIT_BY_REGION.get("기타")
    if region and "서울" in region:
        table = DEPOSIT_BY_REGION["서울"]
    elif region and "부산" in region:
        table = DEPOSIT_BY_REGION["부산"]
    elif region and ("광역시" in region or region in ("대구", "인천", "광주", "대전", "울산")):
        table = DEPOSIT_BY_REGION["광역시"]
    for cap in sorted(table):
        if area <= cap:
            return table[cap]
    return table[999]


def is_public_housing(item: dict) -> bool:
    """국민주택(공공분양)인지. 민영주택과 1순위 요건이 다르다."""
    text = f"{item.get('house_type') or ''} {item.get('name') or ''}"
    return any(word in text for word in ("국민", "공공분양", "신혼희망타운"))


def check_account(profile: dict, item: dict, today: date, person_key: str = "applicant") -> Route:
    """청약통장 순위 판정.

    부부는 각자 청약통장으로 따로 신청할 수 있으므로 사람별로 따로 본다.
    예치금 기준이 거주지에 따라 다르다는 점도 사람마다 갈린다.

    민영주택은 가입기간과 지역·면적별 예치금으로, 국민주택은 가입기간과
    납입 횟수로 1순위가 갈린다. 규제지역(투기과열·청약과열)은 가입 24개월과
    세대주 요건이 더 붙는데, 공고가 규제지역인지는 API 로 확실히 알기 어려워
    24개월을 넘겼는지만 보고 나머지는 공고문 확인으로 넘긴다.
    """
    applicant = profile.get(person_key) or {}
    account = applicant.get("account") or {}
    months = account_months(profile, today, person_key)
    if months is None:
        return Route("청약순위", CONDITIONAL, "통장 가입일 미입력 — 순위 판정 불가")

    period = f"가입 {months // 12}년 {months % 12}개월"

    # 수도권 1순위는 가입 12개월 이상, 규제지역은 24개월 이상
    if months < 12:
        return Route("청약순위", IMPOSSIBLE, f"2순위 ({period} — 12개월 미만)")
    if months < 24:
        return Route("청약순위", CONDITIONAL, f"1순위 ({period}) · 규제지역이면 24개월 필요")

    if is_public_housing(item):
        count = account.get("payment_count")
        if count is None:
            return Route("청약순위", CONDITIONAL, f"국민주택 · {period} · 납입횟수 미입력")
        # 수도권 국민주택 1순위는 납입 12회 이상
        if int(count) < 12:
            return Route("청약순위", IMPOSSIBLE, f"국민주택 2순위 (납입 {count}회, 12회 필요)")

        # 1순위 안에서 당첨자는 순차제로 갈린다. 전용 40㎡ 초과는 저축 '총액',
        # 40㎡ 이하는 납입 '횟수'가 기준이라 같은 1순위라도 승부가 다르다.
        balance = account.get("balance")
        areas = [m["area"] for m in (item.get("matched_models") or []) if m.get("area")]
        over_40 = max(areas) > 40 if areas else True
        tail = ""
        if over_40 and balance is not None:
            tail = f" · 순차제 기준 저축총액 {int(balance) // 10000}만원"
        elif not over_40:
            tail = f" · 순차제 기준 납입 {count}회"
        return Route("청약순위", POSSIBLE, f"국민주택 1순위 ({period} · 납입 {count}회){tail}")

    areas = [m["area"] for m in (item.get("matched_models") or []) if m.get("area")]
    target_area = max(areas) if areas else 85.0
    need = deposit_requirement(applicant.get("region") or "", target_area)
    balance = account.get("balance")
    if balance is None:
        return Route(
            "청약순위",
            CONDITIONAL,
            f"민영 1순위 ({period}) · 전용 {target_area:.1f}㎡ 예치금 {need // 10000}만원 필요"
            " — 잔액 미입력",
        )
    if int(balance) < need:
        return Route(
            "청약순위", IMPOSSIBLE, f"민영 2순위 — 예치금 부족 ({need // 10000}만원 필요)"
        )
    return Route("청약순위", POSSIBLE, f"민영 1순위 ({period} · 예치금 충족)")


CAPITAL_REGIONS = ("서울", "경기", "인천")
# 기타지역 1순위를 함께 받는 광역권 (주택공급규칙). 같은 묶음 거주자는 '기타지역'으로 신청할 수 있다.
WIDE_AREAS = (CAPITAL_REGIONS, ("대전", "세종", "충남"), ("대구", "경북"), ("부산", "울산", "경남"),
              ("광주", "전남"))
# 주택법상 청약 거주지 요건이 없는 유형 (만 19세 이상이면 전국 어디서나)
NO_RESIDENCY = ("임의공급", "오피스텔등", "공공지원민간임대")


def check_region_priority(profile: dict, item: dict, today: date) -> Route:
    """지역우선공급. 신청자 주민등록 주소와 공고의 공급지역을 대조한다.

    공급지역은 반드시 SUBSCRPT_AREA_CODE_NM 기준으로 본다. 공급위치 주소에는
    인접 시·도 지명이 함께 적히는 경우가 있어(인천계양 공고에 '서울특별시
    강서구'가 들어가는 식) 주소로 판정하면 틀린다.
    """
    applicant = profile.get("applicant") or {}
    my_region = (applicant.get("region") or "").strip()
    supply_region = (item.get("area_name") or "").strip()

    if item.get("category") in NO_RESIDENCY:
        return Route("지역우선", POSSIBLE, "거주지 요건 없음 (전국 신청 가능)")
    if not supply_region:
        return Route("지역우선", CONDITIONAL, "공급지역 정보 없음")

    if my_region and supply_region == my_region:
        moved = parse_date(applicant.get("moved_in_date"))
        if not moved:
            return Route("지역우선", CONDITIONAL, f"{supply_region} 해당지역 — 전입일 미입력")
        years = months_between(moved, today) / 12
        if years >= 2:
            return Route("지역우선", POSSIBLE, f"{supply_region} 해당지역 거주 {years:.0f}년")
        # 거주 기간이 짧아도 신청 자체는 된다. 해당지역 우선공급에서 밀릴 뿐이라
        # '확인 필요'로 두면 자격이 없는 것처럼 읽힌다.
        months = months_between(moved, today)
        return Route(
            "지역우선",
            POSSIBLE,
            f"{supply_region} 거주 {months}개월 · 신청 가능하나 해당지역 우선공급"
            "(보통 1~2년 거주)에서는 후순위",
        )

    # 서울 거주자는 경기·인천 물량에 '기타 수도권'으로 신청할 수 있다 (다른 광역권도 같다)
    if any(supply_region in area and my_region in area for area in WIDE_AREAS):
        return Route("지역우선", POSSIBLE, f"{supply_region} 물량에 같은 광역권 기타지역 자격으로 신청 가능")
    if not my_region:
        return Route("지역우선", CONDITIONAL, f"{supply_region} 공고 — 주민등록 시·도 미입력")
    if item.get("category") == "무순위":
        # 무순위는 공고마다 '해당 시·도 거주자'로 제한하기도, 전국으로 열기도 한다
        return Route("지역우선", CONDITIONAL, f"{supply_region} 무순위 — 거주지 제한 여부는 공고문 확인")
    return Route("지역우선", IMPOSSIBLE, f"{supply_region} 공고 — 거주지 요건 미해당")


def income_status(profile: dict, kind: str, tier: str) -> tuple[str, str, float]:
    """특별공급 유형·구간별 소득 요건 판정."""
    household = profile.get("household") or {}
    spouse = profile.get("spouse") or {}

    monthly = income_table.household_income(profile)
    if not monthly:
        # 폼에서 연봉을 건너뛰면 0원이 되어 모든 소득 기준을 '충족'으로 잘못 판정한다
        return CONDITIONAL, "소득 미입력 — 공고문 소득 기준 확인 필요", 0.0
    size = int(household.get("household_size") or 3)
    ratio = income_table.income_ratio(monthly, size)
    dual = bool(income_table.person_monthly(spouse))

    limits = income_table.BRACKETS.get(kind, {}).get(tier)
    if not limits:
        return CONDITIONAL, "소득 기준 미상", ratio
    cap = limits[1] if dual else limits[0]
    if cap is None:
        return IMPOSSIBLE, f"{tier}은 맞벌이 대상 아님", ratio
    # 숫자만 두면 무엇 대비 몇 %인지 알 수 없어, 무엇과 비교했는지 풀어 쓴다
    if ratio <= cap:
        return POSSIBLE, f"가구 소득 {ratio:.0f}% · 기준 {cap}% 이하 충족", ratio
    return IMPOSSIBLE, f"가구 소득 {ratio:.0f}% · 기준 {cap}% 초과", ratio


def check_assets(profile: dict) -> tuple[str, str]:
    """추첨 물량에 적용되는 부동산 자산 요건."""
    applicant = profile.get("applicant") or {}
    spouse = profile.get("spouse") or {}
    values = [
        (applicant.get("assets") or {}).get("real_estate"),
        (spouse.get("assets") or {}).get("real_estate"),
    ]
    if any(v is None for v in values):
        return CONDITIONAL, "부동산 자산 미입력 (3.31억 이하 요건)"
    total = sum(int(v) for v in values)
    if total <= income_table.ASSET_LIMIT_REAL_ESTATE:
        if not total:
            return POSSIBLE, "부동산 없음 (3.31억 이하 충족)"
        return POSSIBLE, f"부동산 {total / 100_000_000:.2f}억 ≤ 3.31억"
    return IMPOSSIBLE, "부동산 자산 3.31억 초과"


# --------------------------------------------------------------------------
# 경로별 판정
# --------------------------------------------------------------------------


def has_special_units(item: dict, label: str) -> bool | None:
    """해당 특별공급 물량이 실제로 있는지. 주택형 정보가 없으면 None(모름)."""
    if not item.get("models"):
        return None
    return bool((item.get("special_counts") or {}).get(label))


def over_seven_years(profile: dict, today: date | None = None) -> bool:
    """혼인신고 7년 초과 — 신혼부부 특별공급 대상이 아님. 신고일을 모르면 False."""
    wed = parse_date((profile.get("household") or {}).get("marriage_date"))
    return bool(wed) and months_between(wed, today or date.today()) >= 84


def newlywed_routes(profile: dict, item: dict, married: bool, today: date | None = None) -> list[Route]:
    """신혼부부 특별공급. 우선/일반/추첨 구간을 각각 본다."""
    routes: list[Route] = []
    if married and over_seven_years(profile, today):
        return [Route("신혼특공", IMPOSSIBLE, "혼인 7년 초과")]

    # 예비 배우자가 입력돼 있으면(폼의 '결혼 예정') 예비신혼부부로 본다. 없으면 해당 없음.
    if not married and not profile.get("spouse"):
        return [Route("신혼특공", IMPOSSIBLE, "혼인 중이거나 예비 배우자가 있어야 신청 가능")]

    units = has_special_units(item, "신혼부부")
    if units is False:
        return [Route("신혼특공", IMPOSSIBLE, "이 공고에 신혼부부 특별공급 물량 없음")]

    # '특별공급'과 그 안의 '일반공급 구간'이 겹쳐 읽히지 않도록 구간 이름을
    # 괄호로 묶어 준다. 예: 신혼부부 특별공급 (일반공급분)
    for tier, label in (("우선공급", "우선공급분"), ("일반공급", "일반공급분"), ("추첨", "추첨분")):
        name = f"신혼부부 특별공급 ({label})"
        status, reason, _ = income_status(profile, "신혼부부특공", tier)
        if status == POSSIBLE:
            asset_status, asset_reason = check_assets(profile)
            if tier == "추첨" and asset_status == IMPOSSIBLE:
                routes.append(Route(name, IMPOSSIBLE, asset_reason))
                continue
            routes.append(Route(name, POSSIBLE, reason))
        else:
            routes.append(Route(name, status, reason))
    return routes


def _public_income(profile: dict, cap: int) -> Route | None:
    """국민주택(공공분양) 특공 소득 기준. 충족이면 None."""
    monthly = income_table.household_income(profile)
    if not monthly:
        return Route("", CONDITIONAL, f"소득 미입력 — 공공분양 소득 {cap}% 기준 확인")
    size = int((profile.get("household") or {}).get("household_size") or 3)
    ratio = income_table.income_ratio(monthly, size)
    if ratio > cap:
        return Route("", IMPOSSIBLE, f"가구 소득 {ratio:.0f}% · 공공분양 기준 {cap}% 초과")
    return None


def _special_base(profile: dict, item: dict, label: str, name: str, need: str) -> Route | None:
    """특공 공통 관문: 물량·무주택·공공분양 소득(120%). 통과하면 None."""
    if has_special_units(item, label) is False:
        return Route(name, IMPOSSIBLE, f"이 공고에 {label} 물량 없음")
    if (profile.get("applicant") or {}).get("homeless") is False:
        return Route(name, IMPOSSIBLE, "무주택 세대구성원이어야 함")
    if is_public_housing(item):
        failed = _public_income(profile, 120)
        if failed:
            return Route(name, failed.status, f"{need} · {failed.reason}")
    return None


def multi_child_routes(profile: dict, item: dict) -> list[Route]:
    """다자녀 특별공급. 2024-03 개정으로 미성년 자녀 2명 이상(태아·입양 포함).

    민영은 소득 기준이 없고, 공공분양은 도시근로자 소득 120% 이하·자산 기준이 붙는다.
    당첨은 배점제(자녀 수·영유아 수·무주택기간·거주기간·통장기간)라 점수는 공고문 표로 계산한다.
    """
    household = profile.get("household") or {}
    minors = household.get("minor_children")
    minors = int(household.get("children") or 0) if minors is None else int(minors)
    if minors < 2:
        return []  # 해당 없는 사람에게 '불가' 줄을 늘어놓지 않는다
    name = "다자녀 특별공급"
    need = f"미성년 자녀 {minors}명"
    blocked = _special_base(profile, item, "다자녀", name, need)
    if blocked:
        return [blocked]
    return [Route(name, POSSIBLE, f"{need} · 무주택 · 통장 6개월 이상이면 신청 · 배점제(자녀 수·무주택기간·거주기간)")]


def elder_parent_routes(profile: dict, item: dict, today: date) -> list[Route]:
    """노부모부양 특별공급. 만 65세 이상 직계존속을 3년 이상 같은 등본에서 부양한 무주택 세대주.

    1순위 통장이 있어야 하고 가점제로 뽑는다. parents_dependents 는 '3년 이상 함께 산' 부모 수.
    """
    household = profile.get("household") or {}
    if not int(household.get("parents_dependents") or 0):
        return []
    name = "노부모부양 특별공급"
    need = "65세 이상 부모 3년 이상 부양"
    blocked = _special_base(profile, item, "노부모부양", name, need)
    if blocked:
        return [blocked]
    if (profile.get("applicant") or {}).get("is_householder") is False:
        return [Route(name, IMPOSSIBLE, "세대주만 신청 가능")]
    account = check_account(profile, item, today)
    if account.status == IMPOSSIBLE:
        return [Route(name, IMPOSSIBLE, f"1순위 통장 필요 — {account.reason}")]
    score, _ = compute_gapjeom(profile, today, bool(household.get("marriage_registered")))
    if household.get("elder_parent_65") is None:
        return [Route(name, CONDITIONAL, f"부모님이 만 65세 이상이면 신청 · 가점 {score}점으로 경쟁")]
    if not household.get("elder_parent_65"):
        return [Route(name, IMPOSSIBLE, "부모님이 만 65세 미만")]
    status = CONDITIONAL if account.status == CONDITIONAL else POSSIBLE
    return [Route(name, status, f"{need} · 무주택 세대주 · 1순위 · 가점 {score}점으로 경쟁")]


def newborn_routes(profile: dict, item: dict, today: date) -> list[Route]:
    """신생아 특별공급. 공고일 기준 2년 이내 출생 자녀(임신 포함)가 있는 무주택 세대.

    공공분양은 income_table 의 신생아특공 구간(우선·일반), 추첨은 소득 대신 자산만 본다.
    민영은 물량만 확인하고 기준은 공고문으로 넘긴다.
    """
    household = profile.get("household") or {}
    if not int(household.get("children") or 0) and not household.get("pregnant"):
        return []
    born = parse_date(household.get("youngest_child_birth"))
    # 막내 생일을 모르면 다 큰 자녀일 수도 있어 싣지 않는다 (가상 인물은 늘 적는다)
    if not household.get("pregnant") and (not born or months_between(born, today) >= 24):
        return []
    if has_special_units(item, "신생아") is False:
        return []
    if (profile.get("applicant") or {}).get("homeless") is False:
        return [Route("신생아 특별공급", IMPOSSIBLE, "무주택 세대구성원이어야 함")]
    when = "임신 중" if household.get("pregnant") else f"막내 생후 {months_between(born, today)}개월"
    if not is_public_housing(item):
        # 민영에도 청약홈이 신생아 물량(NWBB)을 따로 준다. 민영 소득 구간은 확인하지 못해 공고문으로 넘긴다.
        _, _, ratio = income_status(profile, "신생아특공", "우선공급")
        return [Route("신생아 특별공급", CONDITIONAL,
                      f"{when} · 가구 소득 {ratio:.0f}% · 민영 소득·자산 기준은 공고문 확인")]
    routes = []
    for tier, label in (("우선공급", "우선"), ("일반공급", "일반"), ("추첨", "추첨")):
        name = f"신생아 특별공급 ({label})"
        if tier == "추첨":
            status, reason = check_assets(profile)
        else:
            status, reason, _ = income_status(profile, "신생아특공", tier)
        routes.append(Route(name, status, f"{when} · {reason}"))
    return routes


def first_time_routes(profile: dict, item: dict) -> list[Route]:
    """생애최초 특별공급.

    요건이 사람마다 갈린다. 소득세 납부 연수는 개인 이력이라, 부부 중 한 사람만
    5년을 채웠다면 그 사람 명의로 신청하면 된다. 부부 중복청약이 허용되므로
    누가 자격이 되는지 따로 보는 것이 의미가 있다.

    또 '혼인 중이거나 미혼 자녀가 있는 자'여야 한다. 혼인신고 전 무자녀 상태는
    두 사람 모두 신청할 수 없고, 혼인신고와 동시에 열린다.
    """
    if has_special_units(item, "생애최초") is False:
        return [Route("생애최초 특별공급", IMPOSSIBLE, "이 공고에 생애최초 물량 없음")]

    household = profile.get("household") or {}
    married = bool(household.get("marriage_registered"))
    children = int(household.get("children") or 0)
    if not married and not children and not profile.get("spouse") \
            and int(household.get("household_size") or 1) == 1:
        # 1인 가구(단독 세대): 2021-11 부터 추첨 물량 중 전용 60㎡ 이하에만 신청 가능.
        # 부모 등본에 함께 있는 미혼자는 1인 가구가 아니다.
        me = profile.get("applicant") or {}
        name = "생애최초 특별공급 (1인 가구 추첨)"
        if me.get("ever_owned_home") is True:
            return [Route(name, IMPOSSIBLE, "과거 주택 소유 이력 있음")]
        if int(me.get("income_tax_years") or 0) < 5:
            return [Route(name, IMPOSSIBLE, f"소득세 납부 {me.get('income_tax_years') or 0}년 (5년 필요)")]
        areas = [m["area"] for m in (item.get("matched_models") or []) if m.get("area")]
        if areas and min(areas) > 60:
            return [Route(name, IMPOSSIBLE, "1인 가구는 전용 60㎡ 이하만 신청 가능")]
        return [Route(name, CONDITIONAL, "1인 가구 · 추첨 물량·전용 60㎡ 이하만 · 소득·자산 기준은 공고문 확인")]
    if not married and not children:
        return [
            Route(
                "생애최초 특별공급",
                IMPOSSIBLE,
                "혼인 중이거나 자녀가 있어야 신청 가능 — 혼인신고하면 열림",
            )
        ]

    routes: list[Route] = []
    for person_key in ("applicant", "spouse"):
        person = profile.get(person_key) or {}
        if not person:
            continue
        label = person.get("label") or person_key
        suffix = f" ({label} 명의)"

        if person.get("ever_owned_home") is True:
            routes.append(Route(f"생애최초 특별공급{suffix}", IMPOSSIBLE, "과거 주택 소유 이력 있음"))
            continue

        years = person.get("income_tax_years")
        if years is None:
            routes.append(
                Route(f"생애최초 특별공급{suffix}", CONDITIONAL, "소득세 납부 연수 미입력")
            )
            continue
        # 올해분은 연말정산 전이라 소득금액증명원에 안 잡힌다. 딱 1년 모자란
        # 경우는 올해를 쳐 주느냐에 따라 갈리므로 단정하지 않는다.
        if int(years) == 4:
            routes.append(
                Route(
                    f"생애최초 특별공급{suffix}",
                    CONDITIONAL,
                    f"소득세 납부 {years}년 · 올해분이 인정되면 5년 — 공고문·청약홈에서 확인 필요",
                )
            )
            continue
        if int(years) < 4:
            routes.append(
                Route(
                    f"생애최초 특별공급{suffix}",
                    IMPOSSIBLE,
                    f"소득세 납부 {years}년 (5년 필요)",
                )
            )
            continue

        # 이 사람은 개인 요건을 채웠다. 남은 것은 가구 단위 소득 구간이다.
        for tier, tier_label in (("우선공급", "우선공급분"), ("일반공급", "일반공급분"), ("추첨", "추첨분")):
            status, reason, _ = income_status(profile, "생애최초특공", tier)
            routes.append(
                Route(f"생애최초 특별공급 ({tier_label}·{label} 명의)", status, reason)
            )
    return routes


def general_route(profile: dict, item: dict, today: date, married: bool) -> Route:
    """민영주택 일반공급. 소득 요건이 없고 가점·추첨으로 갈린다."""
    score, _ = compute_gapjeom(profile, today, married)
    account = check_account(profile, item, today)
    if account.status == IMPOSSIBLE:
        return Route("일반공급", IMPOSSIBLE, account.reason)
    status = CONDITIONAL if account.status == CONDITIONAL else POSSIBLE
    # strategy.LOW_GAPJEOM 과 같은 기준. 가점이 높은 사람에게 '낮은 편'이라 하지 않는다.
    if score < 40:
        reason = f"소득 조건 없음 · 가점 {score}점은 낮은 편이라 가점제보다 추첨 물량이 현실적"
    else:
        reason = f"소득 조건 없음 · 가점 {score}점 — 가점제·추첨제 모두 노려볼 만함"
    return Route("일반공급 추첨제", status, reason)


def unranked_route(profile: dict, item: dict) -> Route:
    """무순위·잔여세대. 통장·가점 요건이 없거나 매우 느슨하다."""
    applicant = profile.get("applicant") or {}
    if applicant.get("homeless") is False:
        return Route("무순위 (줍줍)", CONDITIONAL, "유주택 — 공고별 조건 확인")
    return Route(
        "무순위 (줍줍)",
        POSSIBLE,
        "계약 취소·미계약분을 다시 푸는 물량 · 무주택 세대구성원이면 신청, 통장 불필요",
    )


def loose_route(profile: dict, item: dict) -> Route:
    """오피스텔·도시형생활주택·임의공급 등 자격 문턱이 낮은 유형.

    이들은 주택법상 '주택'이 아니거나 미분양 물량이라, 청약통장·무주택·거주지
    요건 없이 만 19세 이상이면 신청되는 경우가 대부분이다.

    이름은 반드시 공고가 알려준 실제 주택 유형을 쓴다. 내부 분류명('오피스텔등'
    처럼 여러 유형을 한 API 로 묶어 부르는 이름)이 그대로 나가면 뜻이 통하지 않는다.
    """
    if item["category"] == "임의공급":
        return Route(
            "임의공급",
            POSSIBLE,
            "미분양 물량을 자격 제한 없이 다시 공급하는 방식 · "
            "청약통장·무주택·거주지 요건 없이 만 19세 이상이면 신청",
        )

    kind = (item.get("house_type") or "").strip() or "오피스텔·도시형생활주택"
    return Route(
        f"{kind} 청약",
        POSSIBLE,
        "주택법상 '주택'이 아니라 청약 규제를 받지 않음 · "
        "청약통장·무주택·거주지 요건 없이 만 19세 이상이면 신청",
    )


def public_private_rent_route(profile: dict, item: dict) -> Route:
    """공공지원민간임대. 일반공급은 소득·자산 기준이 없다."""
    applicant = profile.get("applicant") or {}
    if applicant.get("homeless") is False:
        return Route("공공지원민간임대", IMPOSSIBLE, "무주택세대구성원만 신청 가능")
    return Route(
        "공공지원민간임대",
        POSSIBLE,
        "일반공급은 소득·자산 기준 없음 · 만 19세 이상 무주택자면 신청 가능",
    )


# --------------------------------------------------------------------------
# 혼인신고 타이밍 조언
# --------------------------------------------------------------------------


def marriage_advice(profile: dict, item: dict, today: date) -> str:
    household = profile.get("household") or {}
    if household.get("marriage_registered") or not profile.get("spouse"):
        return ""
    if not household.get("marriage_flexible"):
        return ""

    married_score, _ = compute_gapjeom(profile, today, married=True)
    single_score, _ = compute_gapjeom(profile, today, married=False)
    gain = married_score - single_score
    if gain <= 0:
        return ""

    start = item.get("special_begin") or item.get("general_begin")
    if not start:
        text = f"혼인신고 시 가점 +{gain}점"
    elif start <= today:
        # 접수가 이미 시작됐으면 이 공고에는 늦었다. 다음 공고를 위한 안내로 바꾼다.
        text = f"이 공고는 접수 시작({start:%m.%d})이 지남 · 다음 공고 전 혼인신고 시 +{gain}점"
    else:
        days = (start - today).days
        text = f"접수일({start:%m.%d}, {days}일 뒤) 전 혼인신고 시 가점 +{gain}점 (부양가족·무주택기간)"

    # 주소를 나누지 않고 혼인신고하면 가점은 오르지만 특별공급이 막힌다
    home = (profile.get("spouse") or {}).get("household_home") or {}
    if home and not home.get("separated"):
        text += " · 단, 신고 전 배우자 주소 분리해야 특공 유지"
    return text


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


def close_expired_routes(verdict: "Verdict", item: dict, today: date) -> None:
    """접수가 끝난 창구를 신청 불가로 바꾼다.

    특별공급과 일반공급은 접수 기간이 다르다. 특공이 하루만 열리고 일반공급이
    일주일 더 이어지는 공고가 흔한데, 자격만 보고 판정하면 이미 닫힌 창구를
    계속 추천하게 된다.
    """
    windows = (
        ("특별공급", item.get("special_end")),
        ("일반공급 추첨제", item.get("general_end")),
    )
    for keyword, end in windows:
        if not end or end >= today:
            continue
        for route in verdict.routes:
            if keyword in route.name and route.status != IMPOSSIBLE:
                route.status = IMPOSSIBLE
                route.reason = f"접수 마감 ({end:%m.%d})"



# 소형·저가주택 무주택 인정 가격 상한(공시가격). 2024년 개정으로 올랐다는
# 안내가 있으나 확인되지 않아, 개정 전의 낮은 기준을 보수적으로 쓴다.
SMALL_CHEAP_PRICE = {"수도권": 160_000_000, "기타": 100_000_000}
SEPARATE_FIRST = "배우자 주소를 주택 보유 세대원과 분리해야 함"


def is_small_cheap(home: dict) -> bool | None:
    """전용 60㎡ 이하·저가 주택 1호라 민영 일반공급에서 무주택으로 보는지. 모르면 None."""
    price = home.get("price")
    if not price:
        return None
    capital = str(home.get("location") or "") in ("서울", "경기", "인천")
    if int(price) > SMALL_CHEAP_PRICE["수도권" if capital else "기타"]:
        return False
    return None if home.get("area_max_60") is None else bool(home["area_max_60"])


def apply_spouse_household_home(verdict: "Verdict", profile: dict, item: dict, married: bool) -> None:
    """예비 배우자와 같은 주민등록표에 있는 세대원(예: 부모) 명의 주택을 반영한다.

    혼인신고 전에는 배우자의 세대가 우리 세대가 아니라 본인 명의 청약(예비
    신혼부부 특공 포함)에는 영향이 없다. 혼인신고 후에는 배우자의 직계존속이 배우자와 같은 주민등록표에
    있으면 세대구성원이 되어 우리 세대가 유주택이 된다. 그러면 특별공급과
    국민주택은 막히고, 민영 일반공급만 소형·저가주택 예외로 무주택이 인정된다.
    """
    home = (profile.get("spouse") or {}).get("household_home") or {}
    if not home or home.get("separated"):
        return
    public = is_public_housing(item)
    small_cheap = is_small_cheap(home)

    # 혼인신고 전에는 경로마다 적지 않는다. 같은 안내가 혼인신고 조언
    # (marriage_advice)에 한 번 붙고, 카카오톡 한 통 길이 상한이 빠듯하다.
    if not married:
        return
    for index, route in enumerate(verdict.routes):
        if route.status == IMPOSSIBLE:
            continue
        special = route.name.startswith(("신혼", "생애최초"))
        if special or public or small_cheap is False:
            verdict.routes[index] = Route(route.name, IMPOSSIBLE, f"혼인신고 후 유주택 세대 — {SEPARATE_FIRST}")
        elif small_cheap is None:
            verdict.routes[index] = Route(
                route.name, CONDITIONAL,
                f"{route.reason} · 배우자 세대원 집이 전용 60㎡ 이하면 무주택 인정 (확인 필요)",
            )


def apply_own_household_home(verdict: "Verdict", profile: dict, item: dict, married: bool) -> None:
    """본인과 같은 등본에 있는 세대원(예: 부모) 명의 주택. 본인은 무주택이어도 세대가 유주택이다.

    특별공급·국민주택은 무주택세대구성원만 되므로 세대분리해야 한다. 예비 신혼부부 특공은
    혼인으로 만들 새 세대 기준이라 영향이 없다. 민영 일반공급은 소형·저가 예외·추첨 물량이 있어 확인 필요.
    """
    home = (profile.get("applicant") or {}).get("household_home") or {}
    if not home or home.get("separated"):
        return
    owner = home.get("owner") or "세대원"
    public = is_public_housing(item)
    for index, route in enumerate(verdict.routes):
        if route.status == IMPOSSIBLE or (route.name.startswith("신혼") and not married):
            continue
        if public or route.name.startswith(("신혼", "생애최초")):
            verdict.routes[index] = Route(route.name, IMPOSSIBLE, f"본인 등본의 {owner} 주택으로 유주택 세대 — 세대분리해야 신청")
        else:
            verdict.routes[index] = Route(route.name, CONDITIONAL,
                                          f"{route.reason} · 등본에 {owner} 주택 있음 — 가점 무주택기간 0점, 세대분리 권장")


def evaluate(item: dict, profile: dict, today: date | None = None) -> Verdict:
    today = today or date.today()
    household = profile.get("household") or {}
    married = bool(household.get("marriage_registered"))

    verdict = Verdict()
    region = check_region_priority(profile, item, today)

    if item["category"] == "무순위":
        # 무순위는 통장 순위를 따지지 않는다. 통장 없이도 신청되는 경우가 많다.
        verdict.routes = [unranked_route(profile, item)]
        verdict.checks = [region]
        verdict.account = Route("청약순위", POSSIBLE, "무순위 — 통장 순위 무관")
    elif item["category"] == "공공지원민간임대":
        verdict.routes = [public_private_rent_route(profile, item)]
        verdict.checks = [region]
        verdict.account = Route("청약순위", POSSIBLE, "청약통장 불필요 (가점 항목인 공고는 있음)")
    elif item["category"] in ("오피스텔등", "임의공급"):
        verdict.routes = [loose_route(profile, item)]
        verdict.checks = [region]
        verdict.account = Route("청약순위", POSSIBLE, "청약통장 불필요 · 순위 무관")
    else:
        verdict.routes = [
            *newlywed_routes(profile, item, married, today),
            *first_time_routes(profile, item),
            *newborn_routes(profile, item, today),
            *multi_child_routes(profile, item),
            *elder_parent_routes(profile, item, today),
            general_route(profile, item, today, married),
        ]
        verdict.account = check_account(profile, item, today)
        verdict.checks = [verdict.account, region]
        # 배우자 통장이 입력돼 있으면 함께 판정한다. 혼인신고 후에는 부부가
        # 각자 신청할 수 있어 두 사람의 순위가 모두 필요하다.
        verdict.accounts = [(profile.get("applicant", {}).get("label", "나"), verdict.account)]
        if (profile.get("spouse") or {}).get("account", {}).get("opened_date"):
            verdict.accounts.append(
                (
                    profile.get("spouse", {}).get("label", "배우자"),
                    check_account(profile, item, today, "spouse"),
                )
            )

    if item["category"] not in ("오피스텔등", "임의공급", "공공지원민간임대"):
        apply_spouse_household_home(verdict, profile, item, married)
        apply_own_household_home(verdict, profile, item, married)
    close_expired_routes(verdict, item, today)
    verdict.marriage_basis = (
        "혼인신고 완료 · 혼인 7년 초과 기준"
        if married and over_seven_years(profile, today)
        else "혼인신고 완료 · 신혼부부(혼인 7년 이내) 기준"
        if married
        else "혼인신고 전 · 예비 신혼부부 기준"
        if profile.get("spouse")
        else "1인 가구 기준"
    )
    verdict.gapjeom, verdict.gapjeom_detail = compute_gapjeom(profile, today, married)
    verdict.marriage_advice = marriage_advice(profile, item, today)
    _, _, ratio = income_status(profile, "신혼부부특공", "일반공급")
    verdict.income_ratio = ratio
    return verdict


if __name__ == "__main__":
    today = date(2026, 10, 7)
    private = {"category": "APT", "house_type": "민영", "name": "x", "models": [1],
               "special_counts": {"다자녀": 5, "노부모부양": 2}}
    public = {**private, "house_type": "국민", "special_counts": {"신생아": 10, "다자녀": 3}}
    base = {"applicant": {"homeless": True, "is_householder": True, "annual_income": 60_000_000,
                          "account": {"opened_date": "2015-01-01", "balance": 20_000_000, "payment_count": 100}},
            "spouse": {"annual_income": 0}}
    two = {**base, "household": {"children": 2, "household_size": 4, "marriage_registered": True}}
    assert [r.status for r in multi_child_routes(two, private)] == [POSSIBLE]
    assert multi_child_routes({**base, "household": {"children": 1}}, private) == []
    assert multi_child_routes({**base, "household": {"children": 2, "minor_children": 1}}, private) == []
    rich = {**two, "applicant": {**base["applicant"], "annual_income": 200_000_000}}
    assert multi_child_routes(rich, private)[0].status == POSSIBLE      # 민영: 소득 무관
    assert multi_child_routes(rich, public)[0].status == IMPOSSIBLE     # 공공: 120% 초과
    elder = {**base, "household": {"parents_dependents": 1, "elder_parent_65": True, "household_size": 3}}
    assert elder_parent_routes(elder, private, today)[0].status == POSSIBLE
    not_head = {**elder, "applicant": {**base["applicant"], "is_householder": False}}
    assert elder_parent_routes(not_head, private, today)[0].status == IMPOSSIBLE
    assert elder_parent_routes({**base, "household": {}}, private, today) == []
    baby = {**base, "household": {"children": 1, "household_size": 3, "youngest_child_birth": "2026-03-01"}}
    assert newborn_routes(baby, public, today)[0].name == "신생아 특별공급 (우선)"
    assert newborn_routes(baby, private, today) == []                   # 이 공고엔 신생아 물량 없음
    with_nwbb = {**private, "special_counts": {"신생아": 7}}
    assert [(r.name, r.status) for r in newborn_routes(baby, with_nwbb, today)] == [("신생아 특별공급", CONDITIONAL)]
    old = {**base, "household": {"children": 1, "youngest_child_birth": "2023-01-01"}}
    assert newborn_routes(old, public, today) == []
    married_long = {"household": {"marriage_registered": True, "marriage_date": "2012-01-01"}}
    assert over_seven_years(married_long, today) and not over_seven_years({"household": {}}, today)
    single = {"applicant": {"income_tax_years": 9, "ever_owned_home": False}, "household": {}}
    small = {**private, "special_counts": {"생애최초": 3}, "matched_models": [{"area": 59.9}]}
    assert first_time_routes(single, small)[0].status == CONDITIONAL
    assert first_time_routes(single, {**small, "matched_models": [{"area": 84.9}]})[0].status == IMPOSSIBLE
    print("ok")
