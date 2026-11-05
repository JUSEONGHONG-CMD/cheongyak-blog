"""주변 단지의 청약 결과(경쟁률·당첨 가점).

글은 접수 전에 나가서 그 단지의 경쟁률은 아직 없다. 그래서 같은 시·군·구에서 최근 접수가 끝난
단지의 결과를 참고로 보여 준다. 출처: 한국부동산원_청약홈 청약접수 경쟁률 및 특별공급 신청현황
조회 서비스(https://www.data.go.kr/data/15098905/openapi.do). 키는 분양 공고 API와 같다.

CMPET_RATE 는 '11.27'(경쟁률) · '(△17)'(17세대 미달) · '-'(값 없음) 셋 중 하나로 온다.
가점은 추첨·미달이면 '-'나 '0'이다. 값이 없으면 지어내지 않고 뺀다.
"""

from __future__ import annotations

import functools
import re
from datetime import date, timedelta

import requests

from . import applyhome

BASE = "https://api.odcloud.kr/api/ApplyhomeInfoCmpetRtSvc/v1"
TIMEOUT = 20
WINDOW_DAYS = 365
SHOW = 2  # 글에 보여 줄 단지 수
TRY_MAX = 6  # 결과가 없는 단지를 건너뛰며 조회할 최대 단지 수
HOME_AREA = "01"  # RESIDE_SECD: 해당지역
MIN_SUPPLY = 10  # 공급이 이보다 적으면 '1세대 중 1세대 미달'처럼 분위기를 왜곡해 뺀다
AREA_GAP = 20  # 이 단지 평형과 ㎡ 차이가 이보다 크면 비교하지 않는다(84㎡ 단지에 151㎡ 결과가 붙었다)


@functools.cache  # 같은 실행에서 여러 글이 같은 단지를 볼 수 있다
def _rows(operation: str, key: str, house_no: str) -> tuple[dict, ...]:
    response = requests.get(f"{BASE}/{operation}", timeout=TIMEOUT, params={
        "serviceKey": key, "page": 1, "perPage": 200, "cond[HOUSE_MANAGE_NO::EQ]": house_no})
    response.raise_for_status()
    return tuple(response.json().get("data") or [])


def parse_rate(value) -> tuple[float | None, int | None]:
    """(경쟁률, 미달 세대 수). 값이 없으면 둘 다 None."""
    text = str(value or "").strip()
    short = re.fullmatch(r"\(△\s*([\d,]+)\)", text)
    if short:
        return None, int(short.group(1).replace(",", ""))
    try:
        return float(text), None
    except ValueError:
        return None, None


def parse_score(value) -> int | None:
    """가점. 없거나 0이면 None(0은 추첨·기타지역 자리표시)."""
    try:
        return round(float(str(value).strip())) or None
    except ValueError:
        return None


def type_area(house_type) -> float:
    """주택형 '084.9115A' → 84.9115."""
    found = re.match(r"\s*(\d+(?:\.\d+)?)", str(house_type or ""))
    return float(found.group(1)) if found else 0.0


def district(address) -> str:
    """시·군·구까지의 주소. '경기도 수원시 영통구 …' → '경기도 수원시'. 세종은 시·군·구가 없다."""
    parts = str(address or "").split()
    if parts and parts[0].endswith("특별자치시"):
        return parts[0]
    return " ".join(parts[:2])


def summarize(area: float, rates: tuple[dict, ...], scores: tuple[dict, ...]) -> dict | None:
    """1순위 해당지역에서 면적이 가장 가까운 주택형의 결과. 값이 없으면 None."""
    first = [r for r in rates if r.get("RESIDE_SECD") == HOME_AREA and str(r.get("SUBSCRPT_RANK_CODE")) == "1"]
    usable = [r for r in first if parse_rate(r.get("CMPET_RATE")) != (None, None)
              and int(r.get("SUPLY_HSHLDCO") or 0) >= MIN_SUPPLY
              and (not area or abs(type_area(r.get("HOUSE_TY")) - area) <= AREA_GAP)]
    if not usable:
        return None
    row = min(usable, key=lambda r: abs(type_area(r.get("HOUSE_TY")) - area))
    rate, short = parse_rate(row.get("CMPET_RATE"))
    score = next((s for s in scores if s.get("RESIDE_SECD") == HOME_AREA
                  and str(s.get("HOUSE_TY")).strip() == str(row.get("HOUSE_TY")).strip()), {})
    return {"type": str(row.get("HOUSE_TY")).strip(), "supply": row.get("SUPLY_HSHLDCO"), "applied": row.get("REQ_CNT"),
            "rate": rate, "short": short, "low": parse_score(score.get("LWET_SCORE")),
            "average": parse_score(score.get("AVRG_SCORE"))}


def nearby(item: dict, key: str, today: date) -> list[dict]:
    """같은 시·군·구에서 접수가 끝난 최근 단지의 결과. 최근 접수 순."""
    place = district(item.get("address"))
    if not place:
        return []
    area = next((m["area"] for m in item.get("matched_models") or [] if m.get("area")), 0.0)
    notices = applyhome.fetch_operation("getAPTLttotPblancDetail", key, today - timedelta(days=WINDOW_DAYS))
    same = [n for n in notices if district(n.get("HSSPLY_ADRES")) == place
            and str(n.get("HOUSE_MANAGE_NO")) != str(item.get("house_manage_no"))
            and str(n.get("HOUSE_NM")) != str(item.get("name"))
            and "취소분" not in str(n.get("HOUSE_NM"))  # 조합원 취소분은 소수 물량이라 경쟁 분위기를 보여 주지 못한다
            and str(n.get("RCEPT_ENDDE") or "9") < today.isoformat()]
    results: list[dict] = []
    for notice in sorted(same, key=lambda n: str(n.get("RCEPT_ENDDE")), reverse=True)[:TRY_MAX]:
        number = str(notice["HOUSE_MANAGE_NO"])
        result = summarize(area, _rows("getAPTLttotPblancCmpet", key, number),
                           _rows("getAptLttotPblancScore", key, number))
        if result:
            results.append({**result, "name": str(notice.get("HOUSE_NM")).strip(), "ended": str(notice["RCEPT_ENDDE"])})
        if len(results) == SHOW:
            break
    return results


def enrich(item: dict, key: str, today: date) -> bool:
    """item['nearby_results'] 를 채운다. 조회에 실패하면 False(글은 이 칸 없이 나간다)."""
    try:
        item["nearby_results"] = nearby(item, key, today)
    except (requests.RequestException, ValueError) as exc:
        print(f"      주변 단지 경쟁률 조회 실패: {type(exc).__name__}")
        item["nearby_results"] = []
        return False
    return True


def selfcheck() -> None:
    assert parse_rate("11.27") == (11.27, None) and parse_rate("(△17)") == (None, 17)
    assert parse_rate("-") == (None, None) and parse_rate(None) == (None, None)
    assert parse_score("62") == 62 and parse_score("0") is None and parse_score("-") is None
    assert type_area("084.9115A") == 84.9115 and type_area("") == 0.0
    assert district("경기도 수원시 영통구 이의동") == "경기도 수원시" and district("세종특별자치시 산울동 1") == "세종특별자치시"
    # 응답 모양 그대로 (진주 판문지구 레이크써밋 웰가)
    rates = ({"HOUSE_TY": "078.8382 ", "RESIDE_SECD": "01", "SUBSCRPT_RANK_CODE": 1, "SUPLY_HSHLDCO": 60,
              "REQ_CNT": "676", "CMPET_RATE": "11.27"},
             {"HOUSE_TY": "084.9115A", "RESIDE_SECD": "02", "SUBSCRPT_RANK_CODE": 1, "CMPET_RATE": "-"},
             {"HOUSE_TY": "084.9115A", "RESIDE_SECD": "01", "SUBSCRPT_RANK_CODE": 2, "CMPET_RATE": "-"},
             {"HOUSE_TY": "084.9115A", "RESIDE_SECD": "01", "SUBSCRPT_RANK_CODE": 1, "SUPLY_HSHLDCO": 91,
              "REQ_CNT": "5614", "CMPET_RATE": "61.69"})
    scores = ({"HOUSE_TY": "084.9115A", "RESIDE_SECD": "02", "LWET_SCORE": "0", "AVRG_SCORE": "0"},
              {"HOUSE_TY": "084.9115A", "RESIDE_SECD": "01", "LWET_SCORE": "62", "AVRG_SCORE": "66.16"})
    got = summarize(84.0, rates, scores)
    assert (got["type"], got["rate"], got["low"], got["average"]) == ("084.9115A", 61.69, 62, 66), got
    assert summarize(59.0, rates, scores)["rate"] == 11.27
    assert summarize(84.0, (), ()) is None
    assert summarize(151.0, rates, scores) is None  # 평형이 멀면 비교하지 않는다
    tiny = ({**rates[3], "SUPLY_HSHLDCO": 1},)
    assert summarize(84.0, tiny, scores) is None  # 소수 물량은 뺀다
    miss = ({"HOUSE_TY": "059.9986A", "RESIDE_SECD": "01", "SUBSCRPT_RANK_CODE": 1, "SUPLY_HSHLDCO": 179,
             "REQ_CNT": "3", "CMPET_RATE": "(△176)"},)
    got = summarize(59.0, miss, ({"HOUSE_TY": "059.9986A", "RESIDE_SECD": "01", "LWET_SCORE": "-"},))
    assert got["short"] == 176 and got["rate"] is None and got["low"] is None
