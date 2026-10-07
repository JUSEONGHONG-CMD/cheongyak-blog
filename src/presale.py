"""주변에서 최근 분양한 단지의 분양가와 견준다.

실거래 시세는 이미 지어진 집의 값이라 신축 분양가와 곧바로 견주기 어렵다.
같은 동네에서 최근 나온 분양가는 '아직 안 지은 새 아파트'끼리의 비교라 이웃
단지보다 비싸게 나왔는지를 바로 보여준다. 청약홈 API 를 그대로 쓰므로 키가
더 필요 없다.

면적이 단지마다 달라 총액끼리는 비교할 수 없다. 전용 ㎡당 분양가로 바꾼 뒤
이 단지 면적으로 환산해 견준다.
"""

from __future__ import annotations

import statistics
import sys
from datetime import date, timedelta

import requests

from . import applyhome

OPERATION = "getAPTLttotPblancDetail"
MODEL_OPERATION = "getAPTLttotPblancMdl"
LOOKBACK_DAYS = 730
# 비교할 주택형의 전용면적 허용 오차. 한 단지의 주택형이 몇 개 안 돼
# 실거래(±10%)보다 넓게 잡는다.
AREA_TOLERANCE = 0.15
# 같은 구에서 이보다 적게 잡히면 시 전체로 넓힌다 (예: 부천시 오정구 → 부천시)
MIN_NEIGHBORS = 2
# 공고의 HOUSE_DTL_SECD_NM → 메시지에 쓸 이름
KINDS = {"민영": "민간", "국민": "공공"}


def region_key(address: str) -> tuple[str, ...]:
    """'경기도 부천시 오정구 원종동 …' → ('경기', '부천시', '오정구').

    중구·동구·서구처럼 여러 도시에 같은 이름의 구가 있어 시도를 앞에 붙인다.
    시도는 '경기도'와 '경기'처럼 표기가 갈려 앞 두 글자만 쓴다.
    """
    tokens = (address or "").split()
    if not tokens:
        return ()
    parts = [t for t in tokens[1:3] if t.endswith(("시", "군", "구"))]
    return (tokens[0][:2], *parts)


def house_kind(item: dict) -> str:
    """'민영' 또는 '국민'(공공).

    공공분양은 분양가가 민간의 절반 수준이라 섞으면 비교가 무너진다. 무순위·
    임의공급 공고는 이 구분이 비어 있어('불법행위 재공급' 등) 사업주체와
    단지 이름으로 가늠한다.
    """
    kind = item.get("house_type")
    if kind in KINDS:
        return kind
    supplier = str(item.get("supplier") or "")
    name = str(item.get("name") or "")
    if "공사" in supplier or any(word in name for word in ("공공", "신혼희망", "행복주택")):
        return "국민"
    return "민영"


def _per_m2(models: list[dict], area: float) -> float | None:
    """면적이 area 에 가까운 주택형들의 전용 ㎡당 분양가 중위값."""
    low, high = area * (1 - AREA_TOLERANCE), area * (1 + AREA_TOLERANCE)
    rates = [m["price"] / m["area"] for m in models
             if m.get("price") and m.get("area") and low <= m["area"] <= high]
    return statistics.median(rates) if rates else None


def _models(house_manage_no: str, pblanc_no: str, service_key: str, cache) -> list[dict]:
    """과거 공고의 주택형별 (전용면적, 분양가). 이미 나온 분양가는 바뀌지 않아 캐시한다."""
    key = f"presale:{house_manage_no}:{pblanc_no}"
    if cache is not None and (hit := cache.get(key)) is not None:
        return [{"area": a, "price": p} for a, p in hit]
    rows = applyhome.fetch_models(MODEL_OPERATION, service_key, house_manage_no, pblanc_no)
    models = [applyhome.parse_model(row) for row in rows]
    pairs = [[m["area"], m["price"]] for m in models if m["area"] and m["price"]]
    if cache is not None and rows:
        cache.set(key, pairs)
    return [{"area": a, "price": p} for a, p in pairs]


def load_recent(service_key: str, today: date) -> list[dict]:
    """최근 2년 사이 APT 분양 공고."""
    since = today - timedelta(days=LOOKBACK_DAYS)
    try:
        rows = applyhome.fetch_operation(OPERATION, service_key, since)
    except requests.RequestException as exc:
        print(f"[경고] 주변 분양가: 과거 공고 조회 실패: {exc}", file=sys.stderr)
        return []
    recent = []
    for row in rows:
        item = applyhome.normalize(row, "APT")
        if item["notice_date"] and item["notice_date"] >= since:
            item["region"] = region_key(item["address"])
            recent.append(item)
    return recent


def summarize(item: dict, recent: list[dict], service_key: str, cache) -> dict | None:
    models = [m for m in item.get("matched_models") or [] if m.get("area")]
    if not models:
        return None
    area = statistics.median(m["area"] for m in models)
    own_rate = _per_m2(models, area)
    region = region_key(item.get("address") or "")
    if not own_rate or len(region) < 2:
        return None

    kind = house_kind(item)

    def neighbors(key: tuple[str, ...]) -> list[dict]:
        return [r for r in recent
                if r["region"][:len(key)] == key
                and r["house_manage_no"] != item["house_manage_no"]
                and r["house_type"] == kind]

    scope = region
    candidates = neighbors(region)
    if len(candidates) < MIN_NEIGHBORS and len(region) > 2:
        scope = region[:2]
        candidates = neighbors(scope)

    # 같은 단지의 재공고·잔여세대 공고가 따로 잡히므로 이름이 같으면 최근 것만 쓴다
    candidates.sort(key=lambda r: r["notice_date"] or date.min, reverse=True)
    seen_names: set[str] = set()
    found = []
    for other in candidates:
        if other["name"] in seen_names:
            continue
        seen_names.add(other["name"])
        rate = _per_m2(_models(other["house_manage_no"], other["pblanc_no"], service_key, cache), area)
        if rate:
            found.append({
                "name": other["name"],
                "month": other["notice_date"].strftime("%y.%m") if other["notice_date"] else "",
                "price": round(rate * area),
            })
    if not found:
        return None

    median = statistics.median(f["price"] for f in found)
    own = round(own_rate * area)
    return {
        "scope": " ".join(scope[1:]) + ("" if scope == region else " 전체"),
        "kind": KINDS[kind],
        "area": area,
        "count": len(found),
        "median": median,
        "price": own,
        "gap_pct": (own - median) / median * 100,
        "list": found,
    }


def enrich(items: list[dict], service_key: str, cache=None, today: date | None = None) -> None:
    if not items:
        return
    recent = load_recent(service_key, today or date.today())
    for item in items:
        item["presales"] = summarize(item, recent, service_key, cache) if recent else None


def prune(cache, today: date | None = None) -> int:
    """2년이 지나 비교에서 빠진 공고의 캐시를 버린다. 청약홈 공고번호 앞 4자리가 연도다."""
    if cache is None:
        return 0
    oldest = (today or date.today()).year - LOOKBACK_DAYS // 365 - 1
    dropped = 0
    for key in cache.keys():
        if key.startswith("presale:"):
            year = key.split(":")[1][:4]
            if year.isdigit() and int(year) < oldest:
                cache.drop(key)
                dropped += 1
    return dropped
