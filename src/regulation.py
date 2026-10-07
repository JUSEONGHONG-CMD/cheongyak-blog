"""전매제한·실거주 의무 기간 추정.

언제 팔 수 있고 몇 년을 살아야 하는지가 투자 계획의 뼈대라서 공고마다 붙인다.
청약홈 APT 공고에는 규제지역·분양가상한제·공공택지 여부가 Y/N 으로 들어 있어
이것으로 법령 기준표를 적용한다. 무순위·임의공급 공고에는 이 항목이 없다.

기준표는 2023.4 개정 주택법 시행령(전매제한)과 2024.3 개정 주택법(실거주 의무
입주 후 3년 내 시작 유예)을 따른다. 법이 바뀌면 이 파일을 고쳐야 하고, 공고마다
예외가 있어 어디까지나 추정이다. 최종 확인은 모집공고문이다.
"""

from __future__ import annotations

from datetime import date

CAPITAL_AREA = ("서울", "경기", "인천")

# 수도권정비계획법상 과밀억제권역(경기). 서울 전역과 인천(강화·옹진 등 제외)도 포함된다.
OVERCROWDED_GG = (
    "의정부시", "구리시", "하남시", "고양시", "수원시", "성남시", "안양시",
    "부천시", "광명시", "과천시", "의왕시", "군포시", "시흥시",
)
# 남양주시는 이 동들만 과밀억제권역이다.
OVERCROWDED_NAMYANGJU = (
    "호평동", "평내동", "금곡동", "일패동", "이패동", "삼패동",
    "가운동", "수석동", "지금동", "도농동",
)


def _yes(raw: dict, key: str) -> bool:
    return str(raw.get(key) or "").upper() == "Y"


def _overcrowded(region: str, address: str) -> bool:
    if region == "서울":
        return True
    if region == "인천":
        return not any(word in address for word in ("강화군", "옹진군"))
    if "남양주시" in address:
        return any(dong in address for dong in OVERCROWDED_NAMYANGJU)
    return any(city in address for city in OVERCROWDED_GG)


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    # 말일 처리: 8.31 + 6개월 → 2.28
    for last in (31, 30, 29, 28):
        try:
            return day.replace(year=year, month=month, day=min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)


def summarize(item: dict) -> dict | None:
    """규제 정보가 있는 공고만 추정한다. 없으면 None."""
    raw = item.get("raw") or {}
    if raw.get("PARCPRC_ULS_AT") in (None, ""):
        return None

    region = (item.get("area_name") or "").strip()
    address = item.get("address") or ""
    capital = region in CAPITAL_AREA

    speculation = _yes(raw, "SPECLT_RDN_EARTH_AT")
    adjusted = _yes(raw, "MDAT_TRGET_AREA_SECD")
    price_cap = _yes(raw, "PARCPRC_ULS_AT")
    public_land = _yes(raw, "PUBLIC_HOUSE_EARTH_AT") or _yes(raw, "LRSCL_BLDLND_AT")
    regulated = speculation or adjusted

    zones = [name for flag, name in (
        (speculation, "투기과열지구"), (adjusted, "조정대상지역"),
        (price_cap, "분양가상한제"), (public_land, "공공택지"),
    ) if flag]

    if capital:
        if public_land or regulated:
            months = 36
        elif _overcrowded(region, address):
            months = 12
        else:
            months = 6
    else:
        if public_land or regulated:
            months = 12
        elif "광역시" in address:
            months = 6
        else:
            months = 0

    announce = item.get("announce_date")
    resale_until = _add_months(announce, months) if announce and months else None

    # 실거주 의무는 수도권 분양가상한제 주택에만 있다. 기간은 분양가가 인근
    # 시세의 80% 미만이냐로 갈리는데, 그 시세는 국토부가 따로 산정해 공고문에
    # 적으므로 여기서는 범위만 알린다.
    if capital and price_cap:
        residence = "3~5년" if public_land else "2~3년"
    else:
        residence = ""

    return {
        "zones": zones,
        "resale_months": months,
        "resale_until": resale_until,
        "residence": residence,
    }


def enrich(items: list[dict]) -> None:
    for item in items:
        item["regulation"] = summarize(item)
