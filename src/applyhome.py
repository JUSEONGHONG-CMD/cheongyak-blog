"""청약홈 분양정보 수집.

공공데이터포털 「한국부동산원_청약홈 분양정보 조회 서비스」를 호출한다.
  https://www.data.go.kr/data/15098547/openapi.do

오퍼레이션명과 필드명은 실제 응답을 찍어 확인한 값이다(`--probe`). 유형마다
필드 구성이 꽤 다르고 날짜 형식조차 갈리므로(APT 계열은 2026-09-28, 공공지원
민간임대·임의공급은 20260929), 값을 꺼낼 때는 후보 키를 여러 개 두고 첫 번째로
잡히는 것을 쓴다.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
from datetime import date, timedelta

import requests

from .config import DATA_DIR, MissingKey, env, load_json, parse_date

BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"

# 분류명 → (공고 상세 오퍼레이션, 주택형별 오퍼레이션)
# 임의공급은 주택형별 오퍼레이션이 없어 공고 상세 페이지를 읽는다(scrape_models).
OPERATIONS = {
    "APT": ("getAPTLttotPblancDetail", "getAPTLttotPblancMdl"),
    "무순위": ("getRemndrLttotPblancDetail", "getRemndrLttotPblancMdl"),
    "오피스텔등": ("getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl"),
    "공공지원민간임대": ("getPblPvtRentLttotPblancDetail", "getPblPvtRentLttotPblancMdl"),
    "임의공급": ("getOPTLttotPblancDetail", None),
}

# 일반공급 접수 시작일 후보. 순위·지역별로 필드가 쪼개져 있어 전부 보고 가장 이른 날을 쓴다.
GENERAL_BEGIN_KEYS = (
    "RCEPT_BGNDE",
    "GNRL_RCEPT_BGNDE",
    "SUBSCRPT_RCEPT_BGNDE",
    "GNRL_RNK1_CRSPAREA_RCPTDE",
    "GNRL_RNK1_ETC_AREA_RCPTDE",
    "GNRL_RNK1_ETC_GG_RCPTDE",
    "GNRL_RNK2_CRSPAREA_RCPTDE",
    "GNRL_RNK2_ETC_AREA_RCPTDE",
    "GNRL_RNK2_ETC_GG_RCPTDE",
)

# 마감일 후보. RCPTDE(접수일)가 아니라 ENDDE(종료일)만 본다.
GENERAL_END_KEYS = (
    "RCEPT_ENDDE",
    "GNRL_RCEPT_ENDDE",
    "SUBSCRPT_RCEPT_ENDDE",
    "GNRL_RNK1_CRSPAREA_ENDDE",
    "GNRL_RNK1_ETC_AREA_ENDDE",
    "GNRL_RNK1_ETC_GG_ENDDE",
    "GNRL_RNK2_CRSPAREA_ENDDE",
    "GNRL_RNK2_ETC_AREA_ENDDE",
    "GNRL_RNK2_ETC_GG_ENDDE",
)

# 주택형별 응답의 특별공급 세대수 필드. 유형마다 이름이 다르다.
SPECIAL_COUNT_KEYS = {
    "신혼부부": ("NWWDS_HSHLDCO", "SPSPLY_NEW_MRRG_HSHLDCO"),
    "생애최초": ("LFE_FRST_HSHLDCO",),
    "신생아": ("NWBB_HSHLDCO",),
    "다자녀": ("MNYCH_HSHLDCO",),
    "노부모부양": ("OLD_PARNTS_SUPORT_HSHLDCO",),
    "청년": ("YGMN_HSHLDCO", "SPSPLY_YGMN_HSHLDCO"),
    "고령자": ("SPSPLY_AGED_HSHLDCO",),
    "기관추천": ("INSTT_RECOMEND_HSHLDCO",),
}

TIMEOUT = 20
PER_PAGE = 100


def parse_area(value) -> float:
    """주택형 표기에서 전용면적을 뽑아낸다.

    HOUSE_TY 는 '055.0000O' 이나 '084.7646 ' 처럼 타입 문자와 공백이 섞여 오고,
    EXCLUSE_AR 은 '84.9760' 처럼 숫자만 온다. 앞쪽 숫자 부분만 잘라 쓴다.
    """
    if value in (None, "", "-"):
        return 0.0
    match = re.match(r"\s*(\d+(?:\.\d+)?)", str(value))
    return float(match.group(1)) if match else 0.0


def parse_amount(value) -> int:
    """분양가를 원 단위로 바꾼다. API 는 만원 단위 정수로 준다."""
    if value in (None, "", "-"):
        return 0
    try:
        return int(float(str(value).replace(",", ""))) * 10_000
    except ValueError:
        return 0


def _pick(row: dict, *keys, default=None):
    """후보 키 중 처음으로 값이 있는 것을 반환한다."""
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-"):
            return value
    return default


def _pick_dates(row: dict, keys) -> list[date]:
    dates = [parse_date(row.get(key)) for key in keys]
    return [d for d in dates if d]


def _request(operation: str, service_key: str, page: int, conditions: dict | None = None) -> dict:
    params = {"page": page, "perPage": PER_PAGE, "serviceKey": service_key}
    if conditions:
        params.update(conditions)
    response = requests.get(f"{BASE}/{operation}", params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


@functools.cache  # 여러 사람이 같은 실행에서 공유한다. 조회는 실행당 한 번
def fetch_operation(operation: str, service_key: str, since: date) -> list[dict]:
    """한 오퍼레이션의 공고를 since 이후분만 전부 가져온다."""
    conditions = {"cond[RCRIT_PBLANC_DE::GTE]": since.strftime("%Y-%m-%d")}
    rows: list[dict] = []
    page = 1
    while True:
        try:
            payload = _request(operation, service_key, page, conditions)
        except requests.HTTPError as exc:
            # 날짜 형식이 다른 오퍼레이션은 cond 필터를 거부한다. 필터 없이 다시 시도한다.
            if page == 1 and exc.response is not None and exc.response.status_code in (400, 500):
                payload = _request(operation, service_key, page)
                conditions = None
            else:
                raise
        batch = payload.get("data") or []
        rows.extend(batch)
        total = payload.get("matchCount") or payload.get("totalCount") or 0
        if len(rows) >= total or not batch:
            break
        page += 1
    return rows


def normalize(row: dict, category: str) -> dict:
    """유형별로 제각각인 필드를 공통 스키마로 정리한다."""
    begins = _pick_dates(row, GENERAL_BEGIN_KEYS)
    ends = _pick_dates(row, GENERAL_END_KEYS)
    return {
        "category": category,
        "house_manage_no": str(_pick(row, "HOUSE_MANAGE_NO", default="")),
        "pblanc_no": str(_pick(row, "PBLANC_NO", default="")),
        "name": _pick(row, "HOUSE_NM", "BSNS_MBY_NM", default="(이름 없음)"),
        "address": _pick(row, "HSSPLY_ADRES", default=""),
        "area_name": _pick(row, "SUBSCRPT_AREA_CODE_NM", default=""),
        "house_type": _pick(row, "HOUSE_DTL_SECD_NM", "HOUSE_DETAIL_SECD_NM", "HOUSE_SECD_NM", default=""),
        "rent_type": _pick(row, "RENT_SECD_NM", default=""),
        "supplier": _pick(row, "BSNS_MBY_NM", default=""),
        "builder": _pick(row, "CNSTRCT_ENTRPS_NM", default=""),
        "total_units": _pick(row, "TOT_SUPLY_HSHLDCO", default=""),
        "notice_date": parse_date(_pick(row, "RCRIT_PBLANC_DE")),
        "special_begin": parse_date(_pick(row, "SPSPLY_RCEPT_BGNDE")),
        "special_end": parse_date(_pick(row, "SPSPLY_RCEPT_ENDDE")),
        "general_begin": min(begins) if begins else None,
        "general_end": max(ends) if ends else None,
        "announce_date": parse_date(_pick(row, "PRZWNER_PRESNATN_DE")),
        "move_in": _pick(row, "MVN_PREARNGE_YM", default=""),
        # PBLANC_URL 은 청약홈 공고 상세 링크다. HMPG_ADRES 는 분양 홍보 사이트라
        # 공고 내용이 없는 경우가 많으므로 뒤로 미룬다.
        "url": _pick(row, "PBLANC_URL", "HMPG_ADRES", default=""),
        "phone": _pick(row, "MDHS_TELNO", default=""),
        "regulated": _pick(row, "MDAT_TRGET_AREA_SECD", default="N"),
        "speculation_zone": _pick(row, "SPECLT_RDN_EARTH_AT", default="N"),
        "raw": row,
    }


def apply_deadline(item: dict) -> date | None:
    """이 공고에서 가장 늦은 접수 마감일. 마감 여부 판정에 쓴다."""
    candidates = [d for d in (item.get("special_end"), item.get("general_end")) if d]
    return max(candidates) if candidates else None


def in_target_region(item: dict, filters: dict) -> bool:
    """공급지역이 관심 지역인지 판단한다.

    주소 문자열만으로 판단하면 안 된다. 인천계양 공고의 공급위치에는 '경기도
    부천시', '서울특별시 강서구'가 함께 적혀 있어 서울·경기 공고로 오인된다.
    먼저 공급지역 코드명(SUBSCRPT_AREA_CODE_NM: 서울/경기/인천/충남 …)으로
    광역 단위를 가르고, 시·군 필터는 지정한 광역 지역의 공고에만 적용한다.
    """
    supply_region = (item.get("area_name") or "").strip()
    if not supply_region:
        return False
    if supply_region in (filters.get("regions") or []):
        return True
    if supply_region in (filters.get("city_regions") or []):
        address = item.get("address") or ""
        # 지역별로 나눠 적으면 그 지역 목록만 본다. 인천 공고 주소에 '부천'이
        # 함께 적혀 있어도 경기 목록에 걸려 통과하지 않게 하기 위해서다.
        cities = filters.get("cities") or []
        if isinstance(cities, dict):
            cities = cities.get(supply_region) or []
        return any(city and city in address for city in cities)
    return False


# 아파트가 아닌 주택 유형. 공고의 HOUSE_DTL_SECD_NM / HOUSE_SECD_NM 에 이
# 단어가 들어가면 아파트로 보지 않는다. 무순위·임의공급은 대상 건물이 아파트라
# 여기에 넣지 않는다.
NON_APARTMENT_TYPES = ("오피스텔", "도시형", "생활숙박", "생활형")


def is_apartment(item: dict) -> bool:
    """아파트로 볼 수 있는 공고인지.

    아파트가 아닌 유형을 빼는 방식으로 판단한다. 아파트만 골라내려 하면
    '무순위', '임의공급', '불법행위 재공급'처럼 공급 방식을 가리키는 이름이
    유형 자리에 들어와 아파트인데도 빠진다.
    """
    kind = f"{item.get('house_type') or ''} {item.get('name') or ''}"
    return not any(word in kind for word in NON_APARTMENT_TYPES)


def is_excluded_type(item: dict, filters: dict) -> bool:
    """설정에서 빼기로 한 주택 유형인지.

    apartment_only 가 켜져 있으면 아파트가 아닌 유형을 전부 뺀다. 그 밖에
    직접 지정한 유형도 함께 거른다.
    """
    if filters.get("apartment_only") and not is_apartment(item):
        return True
    excluded = filters.get("exclude_house_types") or []
    if not excluded:
        return False
    kind = str(item.get("house_type") or "")
    return any(word and word in kind for word in excluded)


def parse_model(row: dict) -> dict:
    """주택형 한 줄을 정리한다. 전용면적은 EXCLUSE_AR 이 있으면 그것을 쓴다.

    SUPLY_AR 은 공급면적(전용 55㎡ 에 공급 78㎡ 식)이라 전용면적 필터에 쓰면 안 된다.
    """
    area = parse_area(_pick(row, "EXCLUSE_AR", "HOUSE_TY"))
    specials = {}
    for label, keys in SPECIAL_COUNT_KEYS.items():
        value = _pick(row, *keys, default=0)
        try:
            count = int(value)
        except (TypeError, ValueError):
            count = 0
        if count:
            specials[label] = count
    return {
        "area": area,
        # 공급면적. 흔히 말하는 '34평형'은 전용이 아니라 이 값 기준이다.
        "supply_area": parse_area(_pick(row, "SUPLY_AR")),
        "type_name": str(_pick(row, "HOUSE_TY", "TP", default="")).strip(),
        "supply_count": _pick(row, "SUPLY_HSHLDCO", "GNSPLY_HSHLDCO", default=""),
        "special_total": _pick(row, "SPSPLY_HSHLDCO", default=""),
        "specials": specials,
        "price": parse_amount(_pick(row, "LTTOT_TOP_AMOUNT", "SUPLY_AMOUNT")),
    }


@functools.cache
def fetch_models(operation: str, service_key: str, house_manage_no: str, pblanc_no: str) -> list[dict]:
    conditions = {
        "cond[HOUSE_MANAGE_NO::EQ]": house_manage_no,
        "cond[PBLANC_NO::EQ]": pblanc_no,
    }
    try:
        payload = _request(operation, service_key, 1, conditions)
    except requests.RequestException:
        return []
    return payload.get("data") or []


def _cell_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


@functools.cache
def scrape_models(url: str) -> list[dict]:
    """청약홈 공고 상세 페이지의 '공급금액' 표를 주택형 API 응답과 같은 모양으로 읽는다.

    임의공급은 주택형별 API 가 없지만 상세 페이지에는 주택형·세대수·분양가 표가
    있다. 키 이름을 API 와 맞춰 두면 parse_model 을 그대로 쓸 수 있다. 화면이
    바뀌어 표를 못 찾으면 빈 목록을 돌려 예전처럼 정보 없이 넘어간다.
    """
    if not url:
        return []
    try:
        response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[경고] 공고 상세 페이지 조회 실패: {exc}", file=sys.stderr)
        return []

    match = re.search(r"공급금액.*?<table.*?</table>", response.text, re.S)
    if not match:
        return []
    table = match.group(0)
    headers = [_cell_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    columns = {"주택형": "HOUSE_TY", "공급세대수": "SUPLY_HSHLDCO", "분양가": "LTTOT_TOP_AMOUNT"}
    index = {key: headers.index(label) for label, key in columns.items() if label in headers}
    if "HOUSE_TY" not in index:
        return []

    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        cells = [_cell_text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < len(headers):
            continue
        rows.append({key: cells[i] for key, i in index.items()})
    return rows


def area_ok(area: float, filters: dict) -> bool:
    """전용면적이 원하는 범위 안인가. area_ranges(폼 1-4 평형 구간 여러 개)가 있으면 그중 하나라도."""
    ranges = filters.get("area_ranges") or [[filters.get("area_min") or 0, filters.get("area_max") or 9999]]
    return any(float(low) <= area <= float(high) for low, high in ranges)


def attach_models(items: list[dict], service_key: str, filters: dict) -> None:
    """공고에 주택형 정보를 붙이고 면적·분양가 조건 충족 여부를 기록한다."""
    price_cap = filters.get("price_cap")

    for item in items:
        operation = OPERATIONS[item["category"]][1]
        if operation:
            rows = fetch_models(operation, service_key, item["house_manage_no"], item["pblanc_no"])
        else:
            rows = scrape_models(item.get("raw", {}).get("PBLANC_URL") or "")
        models = [parse_model(row) for row in rows]
        matched = [m for m in models if area_ok(m["area"], filters)]
        if price_cap:
            matched = [m for m in matched if not m["price"] or m["price"] <= int(price_cap)]

        item["models"] = models
        item["matched_models"] = matched

        # 면적을 하나도 읽어내지 못했다면 필드 형식이 예상과 다른 것이다.
        # 그럴 때 전부 걸러버리면 알림이 통째로 비므로 통과시키고 넘긴다.
        usable = [m for m in models if m["area"] > 0]
        item["area_match"] = bool(matched) if usable else True

        # 조건에 맞는 주택형들의 특별공급 세대수를 합산한다.
        counts: dict[str, int] = {}
        for model in matched:
            for label, count in model["specials"].items():
                counts[label] = counts.get(label, 0) + count
        item["special_counts"] = counts

        prices = [m["price"] for m in matched if m["price"]]
        item["price_min"] = min(prices) if prices else 0
        item["price_max"] = max(prices) if prices else 0

        # 예산은 걸러내는 기준이 아니라 표시용이다. 가격만 보고 빼면 서울·과천이
        # 통째로 사라지고, 무순위처럼 시세보다 싼 물건도 놓치게 된다.
        budget = filters.get("budget")
        item["over_budget"] = bool(budget and prices and min(prices) > int(budget))


HEALTH_PATH = DATA_DIR / "api_health.json"


def save_health(health: dict) -> None:
    HEALTH_PATH.parent.mkdir(exist_ok=True)
    HEALTH_PATH.write_text(
        json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def collect(profile: dict, service_key: str, today: date | None = None) -> tuple[list[dict], list[str]]:
    """조건에 맞는 공고를 수집한다.

    반환값은 (공고 목록, 문제가 생긴 유형 목록)이다. 청약홈 API 는 점검 등으로
    특정 유형을 통째로 0건으로 내려주는 일이 실제로 있다. 그런 날 "새 공고 없음"
    이라고 알리면 거짓 정보가 되므로, 이전에 받아본 건수와 비교해 이상을 감지한다.
    """
    today = today or date.today()
    filters = profile.get("filters", {})
    lookback = int(filters.get("lookback_days") or 60)
    since = today - timedelta(days=lookback)

    health = load_json(HEALTH_PATH)
    degraded: list[str] = []
    items: list[dict] = []

    for category, (operation, _) in OPERATIONS.items():
        try:
            rows = fetch_operation(operation, service_key, since)
        except requests.RequestException as exc:
            print(f"[경고] {category}({operation}) 수집 실패: {exc}", file=sys.stderr)
            degraded.append(f"{category}(호출 실패)")
            continue

        previous = int(health.get(operation, {}).get("rows") or 0)
        if not rows and previous:
            print(
                f"[경고] {category}: 평소 {previous}건이 오던 자리에 0건입니다. "
                "청약홈 API 점검 중일 수 있습니다.",
                file=sys.stderr,
            )
            degraded.append(f"{category}(0건)")
        elif rows:
            health[operation] = {"rows": len(rows), "checked": today.isoformat()}

        kept = 0
        for row in rows:
            item = normalize(row, category)
            if item["notice_date"] and item["notice_date"] < since:
                continue
            deadline = apply_deadline(item)
            if deadline and deadline < today:
                continue  # 이미 마감
            if not in_target_region(item, filters):
                continue
            if is_excluded_type(item, filters):
                continue
            items.append(item)
            kept += 1
        print(f"      {category:10s} {len(rows):5d}건 중 {kept}건")

    save_health(health)
    attach_models(items, service_key, filters)
    return [item for item in items if item.get("area_match")], degraded


def check_key(service_key: str) -> bool:
    """서비스키가 실제로 동작하는지 한 번 호출해 확인한다."""
    print(f"키 길이 {len(service_key)}자 / 앞 8자리 {service_key[:8]}…")
    if "%" in service_key:
        print("\n⚠ 키에 '%' 가 들어 있습니다. Encoding 키를 복사하신 것 같습니다.")
        print("  마이페이지에서 '일반 인증키 (Decoding)' 값으로 다시 복사해 주세요.")
        return False

    try:
        payload = _request(OPERATIONS["APT"][0], service_key, 1)
    except requests.HTTPError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        status = exc.response.status_code if exc.response is not None else "?"
        print(f"\n✗ 호출 실패 (HTTP {status})")
        if "SERVICE_KEY_IS_NOT_REGISTERED" in body or status in (401, 403):
            print("  키가 아직 반영되지 않았습니다. 승인 후 최대 1시간 걸립니다.")
            print("  잠시 뒤 다시 실행해 보세요. 1시간이 지나도 같으면 키를 다시 복사하세요.")
        else:
            print(f"  응답: {body}")
        return False
    except requests.RequestException as exc:
        print(f"\n✗ 네트워크 오류: {exc}")
        return False

    total = payload.get("matchCount") or payload.get("totalCount") or 0
    rows = payload.get("data") or []
    print(f"\n✓ 키가 정상 동작합니다. APT 분양정보 총 {total}건 조회 가능")
    if rows:
        print(f"  예시: {rows[0].get('HOUSE_NM')}")
    return True


def probe(service_key: str) -> None:
    """각 오퍼레이션을 1건씩 호출해 실제 필드명을 출력한다."""
    DATA_DIR.mkdir(exist_ok=True)
    report = {}
    operations = []
    for category, (detail, model) in OPERATIONS.items():
        operations.append((category, detail))
        if model:
            operations.append((f"{category} 주택형", model))

    for label, operation in operations:
        try:
            payload = _request(operation, service_key, 1)
        except requests.RequestException as exc:
            print(f"✗ {label:16s} {operation}: {exc}")
            report[operation] = {"error": str(exc)}
            continue
        rows = payload.get("data") or []
        total = payload.get("matchCount") or payload.get("totalCount")
        print(f"✓ {label:16s} {operation}: 총 {total}건")
        if rows:
            keys = sorted(rows[0].keys())
            print(f"    필드({len(keys)}개): {', '.join(keys)}")
            report[operation] = {"total": total, "keys": keys, "sample": rows[0]}
        else:
            report[operation] = {"total": total, "keys": [], "sample": None}

    out = DATA_DIR / "probe_fields.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n전체 응답을 {out} 에 저장했습니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description="청약홈 분양정보 수집")
    parser.add_argument("--check", action="store_true", help="서비스키가 동작하는지만 확인")
    parser.add_argument("--probe", action="store_true", help="오퍼레이션별 실제 필드명 확인")
    args = parser.parse_args()

    try:
        service_key = env("ODCLOUD_SERVICE_KEY")
    except MissingKey:
        print("서비스키가 없습니다. 아래 중 한 가지로 넣어 주세요.\n")
        print("  1) .env 파일 (권장)")
        print("     .env.example 을 .env 로 복사한 뒤 ODCLOUD_SERVICE_KEY= 뒤에 값을 붙여넣기")
        print("  2) 이번 터미널에서만")
        print('     $env:ODCLOUD_SERVICE_KEY = "발급받은 Decoding 키"\n')
        print("키는 공공데이터포털 > 마이페이지 > 데이터활용 > Open API >")
        print("활용신청 현황 > 해당 API > '일반 인증키 (Decoding)' 에 있습니다.")
        raise SystemExit(1)

    if args.check:
        raise SystemExit(0 if check_key(service_key) else 1)
    if args.probe:
        probe(service_key)
        return

    from .config import load_profile

    items, degraded = collect(load_profile(), service_key)
    if degraded:
        print(f"\n[주의] 조회가 불완전합니다: {', '.join(degraded)}")
    print(f"\n조건에 맞는 공고 {len(items)}건")
    for item in items:
        print(f"  [{item['category']}] {item['name']} — {item['address']}")


if __name__ == "__main__":
    main()
