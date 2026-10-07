"""주변 아파트 실거래가로 분양가가 싼지 비싼지 가늠한다.

국토교통부 아파트 매매 실거래가 자료를 쓴다.
  https://www.data.go.kr/data/15126469/openapi.do

분양은 아직 없는 집을 미리 사는 것이라, 분양가가 주변 시세보다 낮을수록
안전마진이 크다. 다만 비교 대상은 대부분 기존 아파트라 신축 프리미엄이
반영되지 않는다. 그래서 거래 건물의 평균 건축년도를 함께 남겨, 얼마나 오래된
단지와 견준 값인지 알 수 있게 한다.

과거 거래로 미래 가격을 점치지는 않는다. 추이는 사실로만 적는다.
"""

from __future__ import annotations

import statistics
import sys
import xml.etree.ElementTree as ET
from datetime import date

import requests

BASE = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
REGION_URL = "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json"
TIMEOUT = 20

# 비슷한 크기로 볼 전용면적 허용 오차
AREA_TOLERANCE = 0.10
# 한 점을 낼 때 묶는 개월 수. 한 달치만 보면 거래가 서너 건뿐이라 값이 널뛴다.
RECENT_MONTHS = 3
# 추이 그래프에 찍을 점의 개수. 이번 달을 포함해 13개월이면 1년 전과 견줄 수 있다.
TREND_POINTS = 13
# 신축 분양가는 신축과 견줘야 한다. 이 단지 입주 예정연도에서 이 햇수 안에 지은
# 아파트만 비교군으로 삼고, 최근 거래가 모자라면 다음 단계로 넓힌다(None = 전체).
COHORT_YEARS = (10, 15, None)
MIN_RECENT_TRADES = 10

_reported: set[str] = set()


def _report(detail: str) -> None:
    if detail in _reported:
        return
    _reported.add(detail)
    print(f"[경고] 실거래가 조회: {detail[:200]}", file=sys.stderr)


def region_code(lat: float, lon: float, kakao_key: str, cache=None) -> str | None:
    """좌표 → 시군구 코드(법정동코드 앞 5자리)."""
    key = f"lawd:{lat:.5f},{lon:.5f}"
    if cache and (hit := cache.get(key)) is not None:
        return hit or None
    try:
        response = requests.get(
            REGION_URL,
            headers={"Authorization": f"KakaoAK {kakao_key}"},
            params={"x": lon, "y": lat},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        docs = response.json().get("documents") or []
    except requests.RequestException as exc:
        _report(f"지역코드 변환 실패: {exc}")
        return None

    # region_type 'B' 가 법정동이다. 행정동('H')은 코드 체계가 달라 쓰면 안 된다.
    doc = next((d for d in docs if d.get("region_type") == "B"), None) or (docs[0] if docs else None)
    code = (doc or {}).get("code", "")[:5]
    if cache and code:
        cache.set(key, code)
    return code or None


def month_key(year: int, month: int) -> str:
    return f"{year:04d}{month:02d}"


def months_back(today: date, count: int) -> list[str]:
    """today 기준으로 거슬러 올라가며 count 개월치 YYYYMM 목록."""
    out, year, month = [], today.year, today.month
    for _ in range(count):
        out.append(month_key(year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def fetch_trades(lawd_cd: str, ym: str, service_key: str, cache=None) -> list[dict]:
    """한 시군구·한 달의 아파트 매매 거래 목록."""
    key = f"trade:{lawd_cd}:{ym}"
    if cache and (hit := cache.get(key)) is not None:
        return hit

    try:
        response = requests.get(
            BASE,
            params={"serviceKey": service_key, "LAWD_CD": lawd_cd,
                    "DEAL_YMD": ym, "numOfRows": 1000, "pageNo": 1},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except (requests.RequestException, ET.ParseError) as exc:
        _report(f"{lawd_cd}/{ym}: {exc}")
        return []

    if (root.findtext(".//resultCode") or "") not in ("000", "00"):
        _report(f"{lawd_cd}/{ym}: {root.findtext('.//resultMsg')}")
        return []

    trades = []
    for item in root.iter("item"):
        try:
            amount = int((item.findtext("dealAmount") or "").replace(",", "").strip()) * 10_000
            area = float(item.findtext("excluUseAr") or 0)
        except ValueError:
            continue
        if not amount or not area:
            continue
        # 단지명·법정동은 쓰지 않는다. 한 달치가 수백 건이고 이 목록이 그대로
        # 캐시에 저장되므로, 안 쓰는 항목을 담으면 파일만 무거워진다.
        trades.append({
            "amount": amount,
            "area": area,
            "build_year": int(item.findtext("buildYear") or 0),
        })

    # 과거 달의 거래는 더 늘지 않으므로 캐시해도 안전하다.
    # 이번 달만 아직 늘어날 수 있어 캐시하지 않는다.
    if cache and ym != month_key(date.today().year, date.today().month):
        cache.set(key, trades)
    return trades


def similar(trades: list[dict], area: float) -> list[dict]:
    """전용면적이 비슷한 거래만 고른다."""
    low, high = area * (1 - AREA_TOLERANCE), area * (1 + AREA_TOLERANCE)
    return [t for t in trades if low <= t["area"] <= high]


def summarize(item: dict, service_key: str, kakao_key: str, cache=None,
              today: date | None = None) -> dict | None:
    """공고 하나의 주변 시세 요약. 자료가 모자라면 None."""
    today = today or date.today()
    coords = item.get("coords")
    areas = [m["area"] for m in (item.get("matched_models") or []) if m.get("area")]
    if not coords or not areas:
        return None

    lawd = region_code(coords[0], coords[1], kakao_key, cache)
    if not lawd:
        return None

    target_area = statistics.median(areas)

    # 각 점은 그 달과 앞의 두 달을 묶은 중위값이다. 그래서 받아 와야 하는 달은
    # 점 개수보다 두 달 더 많다. 지난달 이전은 거래가 더 늘지 않아 캐시가 듣고,
    # 같은 시군구의 다른 공고끼리도 이 캐시를 함께 쓴다.
    months = months_back(today, TREND_POINTS + RECENT_MONTHS - 1)
    monthly = {
        ym: similar(fetch_trades(lawd, ym, service_key, cache), target_area)
        for ym in months
    }

    move_in = str(item.get("move_in") or "")
    base_year = int(move_in[:4]) if move_in[:4].isdigit() else today.year
    for years in COHORT_YEARS:
        cutoff = base_year - years if years else 0
        windows = [
            (months[i], [t for ym in months[i:i + RECENT_MONTHS] for t in monthly[ym]
                         if t["build_year"] >= cutoff])
            for i in range(TREND_POINTS)
        ]
        if len(windows[0][1]) >= MIN_RECENT_TRADES:
            break
    recent = windows[0][1]
    if not recent:
        return None

    # 거래가 한 건도 없는 달은 점을 찍지 않는다. 0으로 채우면 폭락한 것처럼 보인다.
    series = [
        {
            "month": ym,
            "median": int(statistics.median(t["amount"] for t in trades)),
            "count": len(trades),
        }
        for ym, trades in windows
        if trades
    ]

    result = {
        "lawd": lawd,
        "area": target_area,
        "median": series[0]["median"],
        "count": series[0]["count"],
        "build_year": round(statistics.median(
            [t["build_year"] for t in recent if t["build_year"]] or [0])),
        "series": series,
        "cohort": f"{cutoff}년 이후 준공" if cutoff else "준공연도 무관",
    }

    # 1년 전 시점은 시계열의 마지막 점이다. 따로 조회하지 않고 같은 값을 쓴다.
    past = next((p for p in series if p["month"] == months[TREND_POINTS - 1]), None)
    if past:
        result["past_median"] = past["median"]
        result["past_count"] = past["count"]
        result["change_pct"] = (result["median"] - past["median"]) / past["median"] * 100

    # 분양가와 견준다. 분양가가 낮을수록 안전마진이 크다.
    price = item.get("price_min") or 0
    if price:
        result["price"] = price
        result["gap"] = price - result["median"]
        result["gap_pct"] = (price - result["median"]) / result["median"] * 100
    return result


def enrich(items: list[dict], service_key: str, kakao_key: str, cache=None,
           today: date | None = None) -> None:
    for item in items:
        item["market"] = summarize(item, service_key, kakao_key, cache, today)


def prune(cache, today: date | None = None) -> int:
    """그래프 구간을 벗어난 달의 실거래 캐시를 버린다. 버린 개수를 돌려준다.

    한 달치가 수백 건이라 그냥 두면 캐시가 달마다 불어난다. 이 파일은 매일
    저장소에 자동 커밋되므로, 쌓이는 만큼 이력도 함께 무거워진다.
    """
    if cache is None:
        return 0
    keep = set(months_back(today or date.today(), TREND_POINTS + RECENT_MONTHS - 1))
    dropped = 0
    for key in cache.keys():
        if key.startswith("trade:") and key.rsplit(":", 1)[-1] not in keep:
            cache.drop(key)
            dropped += 1
    return dropped
