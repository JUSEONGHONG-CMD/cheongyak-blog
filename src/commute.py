"""직장(본인·배우자, 최대 두 곳)까지의 대중교통·자동차 소요시간.

대중교통은 ODsay LAB(https://lab.odsay.com/guide/guide), 자동차는 카카오모빌리티를
쓴다. 결과는 '단지 좌표→직장 좌표' 조합별로 geo_cache 에 남아 모든 사람이 공유한다.
무료 한도를 넘지 않도록 하루 호출 수에 상한을 둔다. 상한에 걸리면 소요시간만
비고 알림은 그대로 나간다.
"""

from __future__ import annotations

import os
import sys
from datetime import date

import requests

from .geo import GeoCache, geocode

BASE = "https://api.odsay.com/v1/api/searchPubTransPathT"
# 자동차 길찾기. 카카오 REST API 키를 그대로 쓴다(별도 발급 불필요).
CAR_BASE = "https://apis-navi.kakaomobility.com/v1/directions"
TIMEOUT = 15

# 소요시간을 구하지 못했을 때 쓰는 값. 순위에서 뒤로 밀리도록 크게 잡는다.
UNKNOWN_MINUTES = 999

# 하루 교통 API(ODsay + 카카오 자동차) 호출 상한. 캐시에 있는 조합은 세지 않는다.
DAILY_LIMIT = int(os.environ.get("TRAFFIC_DAILY_LIMIT") or 500)
# ODsay URI 플랫폼 키는 Referer 도메인으로 인증한다. ODsay 에 등록한 주소와 같아야 한다.
# GitHub Actions 는 IP 가 매번 바뀌어 서버(IP) 키를 못 쓰므로 URI 키를 쓴다.
ODSAY_REFERER = os.environ.get("ODSAY_REFERER") or "https://juseonghong-cmd.github.io"

_reported: set[str] = set()
# ODsay 가 하루 한도 초과(429)를 알리면 그날은 더 부르지 않는다
_odsay_exhausted = False


def _spend(cache: GeoCache | None) -> bool:
    """오늘 호출 수를 하나 올린다. 상한이면 False. 횟수는 공유 캐시에 날짜별로 남는다."""
    if cache is None:
        return True
    key = f"usage:{date.today().isoformat()}"
    used = cache.get(key)
    if used is None:
        for old in [k for k in cache.keys() if k.startswith("usage:")]:
            cache.drop(old)
        used = 0
    if used >= DAILY_LIMIT:
        _report_error(f"하루 교통 API 상한 {DAILY_LIMIT}회 도달 — 나머지는 소요시간 없이 보냅니다")
        return False
    cache.set(key, used + 1)
    return True


def _report_error(detail) -> None:
    """교통 API 오류를 한 번만, 뜻을 풀어서 알린다.

    공고마다 같은 메시지를 반복 출력하면 로그가 묻히므로 한 번만 낸다.
    """
    text = str(detail)
    if text in _reported:
        return
    _reported.add(text)

    print(f"[오류] 교통 소요시간 조회: {text[:200]}", file=sys.stderr)
    if "ApiKeyAuthFailed" in text:
        print(
            "       ODSAY_API_KEY 가 URI 플랫폼 키인지, ODsay LAB > 애플리케이션의\n"
            f"       URI 에 ODSAY_REFERER({ODSAY_REFERER}) 의 도메인이 등록돼 있는지 확인하세요.",
            file=sys.stderr,
        )


def workplace_coords(profile: dict, rest_key: str, cache: GeoCache) -> dict[str, tuple]:
    """설정에 적힌 직장(본인·배우자)의 좌표. 직장을 비워 두면 빈 dict."""
    result = {}
    for person_key in ("applicant", "spouse"):
        person = profile.get(person_key) or {}
        workplace = person.get("workplace") or {}
        query = workplace.get("query") or workplace.get("name")
        if not query:
            continue
        coords = geocode(query, rest_key, cache)
        if coords:
            result[workplace.get("name") or query] = coords
    return result


def transit_minutes(
    origin: tuple[float, float],
    destination: tuple[float, float],
    api_key: str,
    cache: GeoCache | None = None,
) -> int:
    """출발지 → 목적지 대중교통 총 소요시간(분). 실패 시 UNKNOWN_MINUTES."""
    key = f"transit:{origin[0]:.5f},{origin[1]:.5f}->{destination[0]:.5f},{destination[1]:.5f}"
    global _odsay_exhausted
    if cache and (hit := cache.get(key)) is not None:
        return hit
    if not api_key or _odsay_exhausted or not _spend(cache):
        return UNKNOWN_MINUTES

    params = {
        "SX": origin[1],  # 경도
        "SY": origin[0],  # 위도
        "EX": destination[1],
        "EY": destination[0],
        "apiKey": api_key,
        "OPT": 0,  # 0 = 최소 시간 경로
    }
    minutes = UNKNOWN_MINUTES
    try:
        response = requests.get(BASE, params=params, headers={"Referer": ODSAY_REFERER}, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        # ODsay 는 오류도 HTTP 200 으로 주고 본문에 error 를 담는다.
        # 조용히 넘기면 통근시간이 통째로 비는데 이유를 알 수 없게 된다.
        if "error" in payload:
            _report_error(payload["error"])
            _odsay_exhausted = "429" in str(payload["error"])
        paths = (payload.get("result") or {}).get("path") or []
        if paths:
            minutes = int(paths[0]["info"]["totalTime"])
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        _report_error(str(exc))
        minutes = UNKNOWN_MINUTES

    # 실패는 캐시하지 않는다. 한 번 저장하면 원인이 해소돼도 영영 실패한 값을
    # 읽게 된다. 실제로 ODsay 등록 IP 가 바뀐 동안의 실패가 캐시에 굳었다.
    if cache and minutes < UNKNOWN_MINUTES:
        cache.set(key, minutes)
    return minutes


def car_minutes(
    origin: tuple[float, float],
    destination: tuple[float, float],
    kakao_key: str,
    cache: GeoCache | None = None,
) -> int:
    """자동차 소요시간(분). 대중교통 조회가 안 될 때의 대안으로 쓴다."""
    key = f"car:{origin[0]:.5f},{origin[1]:.5f}->{destination[0]:.5f},{destination[1]:.5f}"
    if cache and (hit := cache.get(key)) is not None:
        return hit
    if not _spend(cache):
        return UNKNOWN_MINUTES

    minutes = UNKNOWN_MINUTES
    try:
        response = requests.get(
            CAR_BASE,
            headers={"Authorization": f"KakaoAK {kakao_key}"},
            params={
                "origin": f"{origin[1]},{origin[0]}",
                "destination": f"{destination[1]},{destination[0]}",
                "priority": "RECOMMEND",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        routes = response.json().get("routes") or []
        if routes and routes[0].get("summary"):
            minutes = round(routes[0]["summary"].get("duration", 0) / 60)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        _report_error(f"자동차 길찾기: {exc}")
        minutes = UNKNOWN_MINUTES

    if cache and minutes < UNKNOWN_MINUTES:
        cache.set(key, minutes)
    return minutes


def enrich(items: list[dict], profile: dict, kakao_key: str, odsay_key: str, cache: GeoCache) -> None:
    """각 공고에 직장별 소요시간을 붙인다.

    기본은 대중교통이다. 대중교통 경로를 못 구하는 곳(외곽 산업단지 등)은
    자동차 소요시간으로 대신하고, 어느 기준인지 함께 기록한다.
    """
    destinations = workplace_coords(profile, kakao_key, cache)
    for item in items:
        coords = item.get("coords")
        if not coords or not destinations:
            item["commute"] = {}
            item["commute_car"] = {}
            item["commute_mode"] = ""
            item["commute_total"] = UNKNOWN_MINUTES * 2
            continue

        transit = {
            name: transit_minutes(tuple(coords), dest, odsay_key, cache)
            for name, dest in destinations.items()
        }
        car = {
            name: car_minutes(tuple(coords), dest, kakao_key, cache)
            for name, dest in destinations.items()
        }

        # 순위는 대중교통을 기준으로 삼는다. 매일 오가는 수단이 그쪽이고,
        # 자동차는 거의 항상 빨라 섞어 쓰면 외곽 단지가 부당하게 위로 올라간다.
        # 대중교통 경로 자체가 없는 곳만 자동차로 대신한다.
        usable_transit = any(m < UNKNOWN_MINUTES for m in transit.values())
        ranking_basis = transit if usable_transit else car
        mode = "대중교통" if usable_transit else "자동차"

        item["commute"] = transit
        item["commute_car"] = car
        item["commute_mode"] = mode
        item["commute_total"] = sum(ranking_basis.values())
        item["commute_worst"] = max(ranking_basis.values()) if ranking_basis else UNKNOWN_MINUTES
