"""위치 정보: 주소 → 좌표, 최근접 지하철역·편의시설, 도보 거리.

카카오 로컬 API를 쓴다. https://developers.kakao.com/docs/ko/local/dev-guide
결과는 data/geo_cache.json 에 캐시해 매일 같은 공고를 다시 조회하지 않는다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

from .config import DATA_DIR, load_json

BASE = "https://dapi.kakao.com/v2/local"
TIMEOUT = 10

# 직선거리를 실제 보행거리로 보정하는 계수와 보행 속도(m/분)
DETOUR_FACTOR = 1.3
WALK_SPEED = 67.0

CACHE_PATH = DATA_DIR / "geo_cache.json"

# 주변 편의시설: (아이콘, 카카오 검색 조건, 분류명에 있어야 하는 패턴).
# 대형마트(MT1)엔 '대형슈퍼'(익스프레스 등)도 섞이고, '공원' 검색엔 주차장·공원시설물이 섞여 걸러낸다.
AMENITIES = {
    # 카카오 분류가 틀린 업체(사무실 등)가 섞여 이름으로 한 번 더 거른다
    "대형마트": ("🛒", {"category_group_code": "MT1"}, r"> 대형마트.*\|.*(마트|홈플러스|코스트코|트레이더스|클럽|하나로|마켓)"),
    "공원": ("🌳", {"query": "공원"}, r"> 공원(\s|$)"),
    "초등학교": ("🏫", {"query": "초등학교", "category_group_code": "SC4"}, r"> 초등학교"),
}
NEARBY_RADIUS = 3000


class GeoCache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: dict = load_json(path)

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def keys(self) -> list[str]:
        return list(self.data)

    def drop(self, key: str) -> None:
        self.data.pop(key, None)

    def save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        # 사람이 읽을 파일이 아니고, 실거래가 캐시가 들어오면서 수천 건까지
        # 불어난다. 이 파일은 매일 저장소에 커밋되므로 들여쓰기 한 줄이
        # 그대로 이력 용량이 된다.
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def _headers(rest_key: str) -> dict:
    return {"Authorization": f"KakaoAK {rest_key}"}


_reported: set[str] = set()


def _report_http_error(exc: requests.HTTPError) -> None:
    """카카오 API 오류를 한 번만, 뜻을 풀어서 알린다.

    주소마다 반복 출력하면 로그가 묻히므로 같은 메시지는 한 번만 낸다.
    """
    response = exc.response
    body = response.text if response is not None else ""
    status = response.status_code if response is not None else "?"
    signature = f"{status}:{body[:80]}"
    if signature in _reported:
        return
    _reported.add(signature)

    print(f"[오류] 카카오 로컬 API 호출 실패 (HTTP {status})", file=sys.stderr)
    if "OPEN_MAP_AND_LOCAL" in body:
        print(
            "       앱에서 카카오맵/로컬 서비스가 꺼져 있습니다.\n"
            "       Kakao Developers > 내 애플리케이션 > 앱 설정 > 유료 API >\n"
            "       '사용 가능한 유료 API' 에서 카카오맵을 ON 으로 바꾸세요.",
            file=sys.stderr,
        )
    else:
        print(f"       {body[:200]}", file=sys.stderr)


def geocode(address: str, rest_key: str, cache: GeoCache | None = None, sido: str = "") -> tuple[float, float] | None:
    """주소 → (위도, 경도). 주소 검색이 실패하면 키워드 검색으로 한 번 더 시도한다.

    sido('인천' 등)를 주면 그 시·도 결과만 받는다. 공고 원문을 키워드 검색하면 다른 지방의
    장소가 첫 결과로 오기도 해서다(부평 산곡동 공고 → 충북 제천).
    """
    if not address:
        return None
    key = f"geo:{sido}:{address}" if sido else f"geo:{address}"
    if cache and (hit := cache.get(key)) is not None:
        return tuple(hit) if hit else None

    result = None
    for path, param in (("search/address.json", "query"), ("search/keyword.json", "query")):
        try:
            response = requests.get(
                f"{BASE}/{path}",
                headers=_headers(rest_key),
                params={param: address, "size": 5 if sido else 1},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            documents = response.json().get("documents") or []
        except requests.HTTPError as exc:
            # 설정 문제(권한 미활성화 등)는 주소를 바꿔도 계속 실패하므로 바로 알린다
            _report_http_error(exc)
            documents = []
        except requests.RequestException as exc:
            print(f"[경고] 지오코딩 네트워크 오류: {exc}", file=sys.stderr)
            documents = []
        doc = next((d for d in documents if (d.get("address_name") or "").startswith(sido)), None)
        if doc:
            result = (float(doc["y"]), float(doc["x"]))
            break

    # 실패는 캐시하지 않는다. 저장해 두면 나중에 주소 보정이 개선돼도
    # 캐시된 실패만 계속 읽게 된다.
    if cache and result:
        cache.set(key, list(result))
    return result


def nearest_subway(
    lat: float, lon: float, rest_key: str, cache: GeoCache | None = None, radius: int = 2000
) -> dict | None:
    """반경 내 가장 가까운 지하철역과 도보 거리·시간."""
    key = f"subway:{lat:.5f},{lon:.5f}"
    if cache and (hit := cache.get(key)) is not None:
        return hit or None

    try:
        response = requests.get(
            f"{BASE}/search/category.json",
            headers=_headers(rest_key),
            params={
                "category_group_code": "SW8",
                "x": lon,
                "y": lat,
                "radius": radius,
                "sort": "distance",
                "size": 1,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        documents = response.json().get("documents") or []
    except requests.HTTPError as exc:
        _report_http_error(exc)
        documents = []
    except requests.RequestException as exc:
        print(f"[경고] 지하철역 검색 네트워크 오류: {exc}", file=sys.stderr)
        documents = []

    result = None
    if documents:
        doc = documents[0]
        straight = float(doc.get("distance") or 0)
        walk_distance = round(straight * DETOUR_FACTOR)
        # 카카오는 '남성역 7호선' 처럼 노선명을 붙여 준다. 역 이름만 남긴다.
        place_name = (doc.get("place_name") or "").strip()
        result = {
            "name": place_name.split()[0] if place_name else "",
            "line": " ".join(place_name.split()[1:]),
            "straight_m": round(straight),
            "walk_m": walk_distance,
            "walk_min": max(1, round(walk_distance / WALK_SPEED)),
            "lat": float(doc["y"]),
            "lon": float(doc["x"]),
        }

    if cache and result:
        cache.set(key, result)
    return result


def nearest_place(lat: float, lon: float, label: str, rest_key: str, cache: GeoCache | None = None) -> dict | None:
    """반경 3km 안 가장 가까운 편의시설(AMENITIES 의 label)과 도보 거리. 없으면 None."""
    key = f"near:{label}:{lat:.5f},{lon:.5f}"
    if cache and (hit := cache.get(key)) is not None:
        return hit or None
    _, params, pattern = AMENITIES[label]
    path = "keyword" if "query" in params else "category"
    try:
        response = requests.get(
            f"{BASE}/search/{path}.json",
            headers=_headers(rest_key),
            params={**params, "x": lon, "y": lat, "radius": NEARBY_RADIUS, "sort": "distance", "size": 15},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        documents = response.json().get("documents") or []
    except requests.HTTPError as exc:
        _report_http_error(exc)
        return None
    except requests.RequestException as exc:
        print(f"[경고] {label} 검색 네트워크 오류: {exc}", file=sys.stderr)
        return None
    doc = next((d for d in documents if re.search(pattern, f"{d.get('category_name')}|{d.get('place_name')}")), None)
    result = {"name": doc["place_name"], "walk_m": round(float(doc["distance"]) * DETOUR_FACTOR)} if doc else {}
    if cache:
        cache.set(key, result)  # '3km 안에 없음'도 저장한다 (오류는 저장하지 않음)
    return result or None


def nearby_text(coords, rest_key: str, cache: GeoCache | None = None, subway: bool = True) -> str:
    """'🚉 율전역 도보 8분 · 🛒 롯데마트 천천점 1.4km · 🌳 밤나무동산 980m'. 좌표가 없으면 빈 문자열."""
    if not coords or not rest_key:
        return ""
    parts = []
    station = nearest_subway(coords[0], coords[1], rest_key, cache) if subway else None
    if station:
        parts.append(f"🚉 {station['name']} 도보 {station['walk_min']}분")
    for label, (icon, _, _) in AMENITIES.items():
        place = nearest_place(coords[0], coords[1], label, rest_key, cache)
        if place:
            parts.append(f"{icon} {place['name']} {_distance(place['walk_m'])}")
    return " · ".join(parts)


def _distance(meters: int) -> str:
    return f"{meters / 1000:.1f}km" if meters >= 1000 else f"{round(meters, -1)}m"


def address_candidates(address: str, name: str = "") -> list[str]:
    """지오코딩에 차례로 시도해 볼 주소 후보들.

    공고의 공급위치는 형식이 제각각이다. '김포 풍무역세권 C5블록 (경기도 김포시
    사우동 527-1 일원)' 처럼 **정작 쓸모있는 번지가 괄호 안에** 들어 있기도 하고,
    '경기도 용인시 처인구 원삼면 … 일반산업단지 D1-1BL' 처럼 번지 자체가 없기도
    하다. 그래서 원문 → 괄호 안 → 단지명 → 행정구역만 남긴 축약 순으로 시도한다.
    """
    address = (address or "").strip()
    candidates = [address] if address else []

    # '인천 계양구 귤현동, 동양동, …, 경기도 부천시 대장동, 서울 강서구 …' 처럼
    # 여러 지역을 늘어놓은 공고는 원문으로 찾으면 엉뚱한 지역이 잡힌다.
    # 맨 앞이 공급지역이므로 첫 쉼표 앞을 먼저 찾는다.
    head = address.split(",")[0].strip()
    if "," in address and head:
        candidates.insert(0, head)

    # 괄호 안에는 실제 지번 주소(김포 예)가 들기도, 사업명이 들기도 한다. 사업명은 키워드 검색에서
    # 다른 지방 장소가 걸리므로('산곡6 주택재개발정비사업' → 제천), 주소처럼 생긴 괄호 → 괄호·'일원'을
    # 뺀 원문 → 나머지 괄호 순으로 본다.
    inners = [i.strip() for i in re.findall(r"\(([^)]*)\)", address) if i.strip()]
    looks_address = [i for i in inners if re.search(r"\S(시|군|구|동|읍|면|리)\s", i + " ")]
    bare = re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", address).replace("일원", " ")).strip()
    for candidate in (*looks_address, bare, *inners):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    if name and name not in candidates:
        candidates.append(name)

    # 마지막 수단: 행정구역 토큰만 남겨 동네 좌표라도 얻는다.
    # 괄호를 지우지 않고 원문 전체에서 뽑아야 괄호 안 주소도 살릴 수 있다.
    tokens = re.sub(r"[()]", " ", address).split()
    admin = [t for t in tokens if t.endswith(("도", "시", "군", "구", "읍", "면", "동", "리"))]
    simplified = " ".join(admin[:4])
    if simplified and simplified not in candidates:
        candidates.append(simplified)

    return candidates


def enrich(items: list[dict], rest_key: str, cache: GeoCache) -> None:
    """공고 목록에 좌표와 최근접 지하철역 정보를 붙인다."""
    for item in items:
        candidates = address_candidates(item.get("address") or "", item.get("name") or "")
        coords = None
        for index, candidate in enumerate(candidates):
            coords = geocode(candidate, rest_key, cache, (item.get("area_name") or "").strip())
            if coords:
                # 원문이 아닌 후보로 찾았다면 좌표가 대략적이라는 뜻이다
                item["coords_approximate"] = index > 0
                break
        item["coords"] = coords
        item["subway"] = (
            nearest_subway(coords[0], coords[1], rest_key, cache) if coords else None
        )
