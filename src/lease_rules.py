"""LH 임대 유형별 자격 규칙.

기준은 2026-09~10 LH 공고문(국민임대 태백소도, 행복주택 인천석남, 통합공공임대 대구연호,
영구임대 공주옥룡1, 매입 신혼·신생아Ⅰ 대구경북, 고령자·다자녀 매입 인천, 전세임대 신혼Ⅱ·다자녀·든든주택)
에서 읽은 소득·자산 표다. 매년(보통 3~4월) 기준 금액이 바뀌므로 그때 아래 표를 갱신한다.

판정은 세 가지다: YES(가능), MAYBE(확인 필요), NO(해당 안 됨). 폼은 연봉을 '구간'으로 받으므로
구간이 기준선에 걸치면 MAYBE 다. 총자산은 예금 등 금융자산까지 합한 값인데 폼은 부동산·자동차만
받으므로, 그 둘의 합이 기준을 넘을 때만 NO 로 보고 나머지는 공고문 확인으로 남긴다.

수급자·차상위·지원대상 한부모·장애인 같은 자격은 폼에서 묻지 않으므로, 그런 사람만 넣을 수 있는
공고(전세임대 청년 1순위, 기존주택 전세임대, 집주인임대 등)는 알림에서 뺀다.
"""

from __future__ import annotations

import re
from datetime import date

from . import income_table
from .config import parse_date

YES, MAYBE, NO = "가능", "확인 필요", "해당 안 됨"

# 전년도 도시근로자 가구원수별 월평균소득 100%(가산 전). 국민임대 70% 표(1인 2,669,354)에서 역산.
URBAN_100 = {1: 3_813_363, 2: 5_866_270, 3: 8_168_429, 4: 8_802_202, 5: 9_326_985, 6: 9_906_263}
URBAN_ADD = {1: 20, 2: 10}  # 1인 +20%p, 2인 +10%p 가산
# 2026 기준 중위소득 100% (통합공공임대 공고 표)
MEDIAN_100 = {1: 2_564_238, 2: 4_199_292, 3: 5_359_036, 4: 6_494_738, 5: 7_556_719, 6: 8_555_952}
CAR_LIMIT = 45_420_000
ASSET = {"국민": 345_000_000, "영구": 245_000_000, "행복청년": 251_000_000, "신혼Ⅱ": 362_000_000}

SIDO = {
    "서울": "서울", "경기": "경기", "인천": "인천", "부산": "부산", "대구": "대구", "광주": "광주",
    "대전": "대전", "울산": "울산", "세종": "세종", "강원": "강원", "충청북": "충북", "충북": "충북",
    "충청남": "충남", "충남": "충남", "전라북": "전북", "전북": "전북", "전라남": "전남", "전남": "전남",
    "경상북": "경북", "경북": "경북", "경상남": "경남", "경남": "경남", "제주": "제주",
}


def sido(text: str) -> str:
    """'경상북도 외'·'전북특별자치도' → '경북'·'전북'. 모르면 빈 문자열. '전국'은 '전국'."""
    text = (text or "").strip()
    if text.startswith("전국"):
        return "전국"
    return next((short for key, short in SIDO.items() if text.startswith(key)), "")


def classify(ais_name: str, name: str) -> str:
    """LH 공고 → 알림에서 다루는 유형 이름. 다루지 않으면 빈 문자열."""
    if "창업지원" in name or "장애인" in name:  # 창업자·장애인 전용 — 폼에서 묻지 않는다
        return ""
    if "행복주택" in ais_name or "행복주택" in name:
        # 신혼희망타운 안의 행복주택은 신혼·예비신혼·한부모만 넣을 수 있다
        return "행복주택(신혼희망)" if "신혼희망" in ais_name + name else "행복주택"
    for key in ("국민임대", "영구임대", "통합공공임대"):
        if key in ais_name:
            return key
    if "공공임대" in ais_name:
        return "공공임대(분양전환)"
    if ais_name == "매입임대":
        if "든든전세" in name:
            return "든든전세"
        if "전세형" in name and ("신혼" in name or "신생아" in name):
            return "신혼·신생아Ⅱ"
        # 'Ⅱ' 대신 'II'·'신생아2' 로 적는 공고가 있다. '26년' 같은 연도의 2 는 빼고 본다.
        if ("신혼" in name or "신생아" in name) and not re.search(r"II|Ⅱ|(아|대)\s*2(?!\d)", name):
            return "신혼·신생아Ⅰ"
        if "청년" in name or "기숙사" in name:
            return "청년"
        if "고령자" in name:
            return "고령자 매입임대"
        if "다자녀" in name:
            return "다자녀"
        return "일반 매입임대"
    if ais_name == "전세임대":
        if "든든주택" in name:
            return "든든주택(전세)"
        if "신혼" in name or "신생아" in name:
            return "신혼·신생아Ⅱ" if re.search(r"II|Ⅱ", name) else "신혼·신생아Ⅰ"
        if "다자녀" in name:
            return "다자녀"
        if "청년" in name and "1순위" not in name:
            return "청년"
    return ""  # 가정어린이집·공공분양(신혼희망)·집주인임대·수급자 전용 전세임대 등


def urban(pct: int, size: int) -> float:
    size = min(max(size, 1), 6)
    return URBAN_100[size] * (pct + URBAN_ADD.get(size, 0)) / 100


def median(pct: int, size: int) -> float:
    return MEDIAN_100[min(max(size, 1), 6)] * pct / 100


def _cap(rng, cap: float) -> str:
    """소득 구간(최소, 최대)이 기준 이하인가. 모르면 MAYBE."""
    if not rng:
        return MAYBE
    low, high = rng
    return YES if high <= cap else NO if low > cap else MAYBE


def _worst(*statuses: str) -> str:
    return NO if NO in statuses else MAYBE if MAYBE in statuses else YES


def facts(profile: dict, today: date) -> dict:
    """판정에 쓰는 사실들. 값이 없으면 모름(None)으로 둔다."""
    household = profile.get("household") or {}
    me, spouse = profile.get("applicant") or {}, profile.get("spouse") or {}
    married = bool(household.get("marriage_registered"))
    wed = parse_date(household.get("marriage_date"))
    children = int(household.get("children") or 0)
    rng = household.get("income_range") or _range_of(household.get("monthly_income"), "household")
    if not rng:
        total = income_table.household_income(profile)
        rng = (total, total) if total else None

    def age(person):
        birth = parse_date(person.get("birth_date"))
        if birth:
            return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        return int(person["age"]) if person.get("age") else None

    assets = sum(int((p.get("assets") or {}).get("real_estate") or 0) + int((p.get("assets") or {}).get("car") or 0)
                 for p in (me, spouse))
    car = max(int((p.get("assets") or {}).get("car") or 0) for p in (me, spouse))
    mine = me.get("income_range") or _range_of(me.get("monthly_income"), "person")
    if not mine and (me.get("monthly_income") is not None or me.get("annual_income")):
        mine = (income_table.person_monthly(me),) * 2
    return {
        "size": int(household.get("household_size") or (2 if spouse else 1)),
        "income": rng,
        "my_income": mine,  # 본인 소득 (청년 계층은 본인 기준)
        "dual": bool(income_table.person_monthly(spouse)),
        "newlywed": (married and (wed is None or (today - wed).days <= 7 * 365)) or (bool(spouse) and not married),
        "single_parent": not married and not spouse and children > 0,
        "children": children,
        "age": age(me),
        "homeless": me.get("homeless") is not False,  # 본인
        # 같은 등본(세대)의 부모 등 명의 집. 있으면 '무주택세대구성원'이 아니다.
        "household_ok": not separated_home(me),
        "engaged": bool(spouse) and not married,  # 예비신혼은 혼인으로 만들 새 세대 기준
        "assets": assets,
        "car": car,
    }


def separated_home(person: dict) -> dict:
    """이 사람과 같은 등본에 있는 세대원(부모 등) 명의 집. 세대분리돼 있거나 없으면 빈 dict."""
    home = person.get("household_home") or {}
    return {} if not home or home.get("separated") else home


def _range_of(value, which: str):
    """구간 대표값만 있는 예전 프로필(범위 항목 이전)에서 구간을 되찾는다. 대표값이 아니면 None."""
    from . import import_form  # 폼 구간표는 한 곳(import_form)에만 둔다
    table = (import_form.HOUSEHOLD_INCOME, import_form.HOUSEHOLD_RANGE) if which == "household" \
        else (import_form.PERSON_INCOME, import_form.PERSON_RANGE)
    label = next((k for k, v in table[0].items() if v == value and value is not None), None)
    return table[1][label] if label else None


def _assets(f: dict, limit: int) -> str:
    return NO if f["assets"] > limit or f["car"] > CAR_LIMIT else YES


def judge(kind: str, profile: dict, today: date, relaxed: bool = False) -> list[tuple[str, str]]:
    """[(전형, 판정)] — 넣을 수 있는(가능·확인 필요) 전형만. relaxed 는 '입주자격완화' 공고(소득·자산 배제)."""
    f = facts(profile, today)
    if not f["homeless"]:
        return []
    out: list[tuple[str, str]] = []

    def add(track: str, *statuses: str, cap_maybe: bool = False, own: bool = False):
        """own: 본인 무주택만 보는 전형(청년·예비신혼). 아니면 세대 전체가 무주택이어야 한다."""
        status = _worst(*statuses)
        if cap_maybe and status == YES:
            status = MAYBE
        if not own and not f["household_ok"] and status != NO:
            status, track = MAYBE, f"{track} · 세대분리 필요"
        if status != NO:
            out.append((track, status))

    size, inc = f["size"], f["income"]
    if kind == "국민임대":
        add("일반(소득 70% 이하)", _cap(inc, urban(70, size)), _assets(f, ASSET["국민"]))
    elif kind == "영구임대":
        # 수급자 등이 먼저고 일반은 소득 50% 이하만, 남는 경우에 공급된다
        add("일반(소득 50% 이하)", _cap(inc, urban(50, size)), _assets(f, ASSET["영구"]), cap_maybe=True)
    elif kind.startswith("행복주택"):
        newlywed_only = kind == "행복주택(신혼희망)"
        income = (lambda rng, cap: YES) if relaxed else _cap
        assets = (lambda limit: NO if f["car"] > CAR_LIMIT else YES) if relaxed else (lambda limit: _assets(f, limit))
        if not newlywed_only and f["age"] is not None and 19 <= f["age"] <= 39:
            add("청년 계층", income(f["my_income"], urban(100, 1)), assets(ASSET["행복청년"]), own=True)
        if f["newlywed"] or f["single_parent"]:
            add("신혼부부·한부모 계층", income(inc, urban(120 if f["dual"] else 100, size)), assets(ASSET["국민"]),
                own=f["engaged"])
        if not newlywed_only and f["age"] is not None and f["age"] >= 65:
            add("고령자 계층", income(inc, urban(100, size)), assets(ASSET["국민"]))
    elif kind == "통합공공임대":
        bonus = 30 if f["newlywed"] and f["dual"] else 0
        add("일반공급(중위소득 150% 이하)", _cap(inc, median(150 + bonus, size)), _assets(f, ASSET["국민"]))
    elif kind == "공공임대(분양전환)":
        add("일반공급(청약통장 순위·소득 기준은 공고문 확인)", MAYBE)
    elif kind == "신혼·신생아Ⅰ":
        if f["newlywed"] or f["single_parent"]:
            add("신혼·신생아Ⅰ(소득 70%, 맞벌이 90%)", _cap(inc, urban(90 if f["dual"] else 70, size)),
                _assets(f, ASSET["국민"]), own=f["engaged"])
    elif kind == "일반 매입임대":
        # 1순위는 수급자 등, 일반은 2순위(소득 100% 이하)
        add("2순위(소득 100% 이하)", _cap(inc, urban(100, size)), _assets(f, ASSET["영구"]), cap_maybe=True)
    elif kind == "고령자 매입임대":
        if f["age"] is not None and f["age"] >= 65:
            add("고령자(소득 100% 이하)", _cap(inc, urban(100, size)), _assets(f, ASSET["영구"]), cap_maybe=True)
    elif kind == "다자녀":
        if f["children"] >= 2:
            add("다자녀(소득 70% 이하)", _cap(inc, urban(70, size)), _assets(f, ASSET["국민"]))
    elif kind == "든든주택(전세)":
        if f["newlywed"] or f["children"] >= 2:
            add("신혼·예비신혼 또는 다자녀(소득·자산 기준 없음)", YES, own=f["engaged"])
    return out


if __name__ == "__main__":
    today = date(2026, 10, 7)
    assert classify("국민임대", "부천시 국민임대") == "국민임대"
    assert classify("행복주택", "인천석남 행복주택") == "행복주택"
    assert classify("행복주택(신혼희망)", "하남 신혼희망타운 행복주택") == "행복주택(신혼희망)"
    assert classify("매입임대", "3월 신혼·신생아 I 매입임대주택") == "신혼·신생아Ⅰ"
    assert classify("매입임대", "[서울지역본부] 26년 3차 신혼·신생아 매입임대Ⅰ 예비입주자") == "신혼·신생아Ⅰ"
    assert classify("매입임대", "신혼·신생아Ⅱ(전세형)") == "신혼·신생아Ⅱ"
    assert classify("전세임대", "청년 전세임대 1순위 입주자 수시모집") == ""
    assert classify("행복주택", "인천논현 LH16단지 창업지원주택 입주자 모집") == ""
    assert classify("매입임대", "장애인 자립특화형 주택 입주자 모집") == ""
    assert classify("가정어린이집", "가정어린이집 운영예정자") == ""
    assert classify("공공분양(신혼희망)", "신혼희망타운 입주자모집") == ""
    assert sido("경상북도 외") == "경북" and sido("전북특별자치도") == "전북" and sido("전국") == "전국"
    # 2인 100% = 6,452,897 (가산 10%p), 국민 2인 70% = 4,693,016 — 공고문 표와 같아야 한다
    assert round(urban(100, 2)) == 6_452_897 and round(urban(70, 2)) == 4_693_016 and round(urban(70, 1)) == 3_432_027
    single = {"applicant": {"birth_date": "1996-07-21", "homeless": True, "income_range": [2_666_000, 4_500_000]},
              "household": {"household_size": 1, "income_range": [0, 4_666_000]}}
    assert judge("행복주택", single, today) == [("청년 계층", YES)]                    # 본인 450만 ≤ 457.6만
    assert judge("국민임대", single, today) == [("일반(소득 70% 이하)", MAYBE)]       # 0~466만이 343만에 걸침
    assert judge("국민임대", {**single, "household": {"household_size": 1, "income_range": [4_666_000, 8_166_000]}}, today) == []
    assert judge("신혼·신생아Ⅰ", single, today) == [] and judge("행복주택(신혼희망)", single, today) == []
    assert judge("국민임대", {**single, "applicant": {"homeless": False}}, today) == []
    couple = {"applicant": {"age": 33, "monthly_income": 6_100_000}, "spouse": {"monthly_income": 3_000_000},
              "household": {"household_size": 2, "income_range": [4_666_000, 8_166_000]}}
    # 맞벌이 예비부부 2인: 행복 신혼 120% = 703.9만 (466~816만에 걸침), 통합 150%+30%p = 755.9만 (걸침)
    assert judge("행복주택", couple, today) == [("신혼부부·한부모 계층", MAYBE)]
    assert judge("통합공공임대", couple, today) == [("일반공급(중위소득 150% 이하)", MAYBE)]
    rich = {**couple, "household": {"household_size": 2, "income_range": [9_750_000, 10_583_000]}}
    assert judge("통합공공임대", rich, today) == [] and judge("든든주택(전세)", rich, today) == [("신혼·예비신혼 또는 다자녀(소득·자산 기준 없음)", YES)]
    assert judge("행복주택", {**single, "household": {"household_size": 1, "income_range": [16_333_000, 99e9]}}, today, relaxed=True) == [("청년 계층", YES)]
    assert judge("국민임대", {**single, "applicant": {"homeless": True, "assets": {"real_estate": 400_000_000}}}, today) == []
    # 부모와 같은 세대(부모 집 있음): 청년 계층은 그대로, 세대 기준 유형은 '세대분리 필요'
    with_parents = {**single, "applicant": {**single["applicant"], "household_home": {"owner": "부모", "separated": False}}}
    assert judge("행복주택", with_parents, today) == [("청년 계층", YES)]
    assert judge("국민임대", with_parents, today) == [("일반(소득 70% 이하) · 세대분리 필요", MAYBE)]
    split = {**single, "applicant": {**single["applicant"], "household_home": {"owner": "부모", "separated": True}}}
    assert judge("국민임대", split, today) == judge("국민임대", single, today)
    old = {"applicant": {"age": 30, "homeless": True}, "household": {"household_size": 1, "monthly_income": 4_666_000}}
    assert facts(old, today)["income"] == [0, 4_666_667]
    print("ok")
