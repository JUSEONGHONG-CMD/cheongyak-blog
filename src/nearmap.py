"""단지 주변 지도. 썸네일 배경과 본문 '주변에 뭐가 있을까' 칸에 쓴다.

지도는 OpenStreetMap 타일이다. 카카오·네이버 지도는 이미지를 저장·가공해 게시해도 되는지 확인되지 않아 쓰지 않는다.
타일 이용 정책(https://operations.osmfoundation.org/policies/tiles/)에 맞춰 프로그램 이름을 밝히고, 실행 안에서
같은 타일은 다시 받지 않으며, 글에는 '© OpenStreetMap 기여자'를 표시한다. 대량으로 받지 않는다(글 하나에 수십 장).
좌표와 주변 시설은 지금까지처럼 카카오 로컬 API(geo.py)로 찾는다.
"""

from __future__ import annotations

import functools
import io
import math

import requests
from PIL import Image, ImageDraw, ImageFont

from . import geo

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
HEADERS = {"User-Agent": "cheongyak-blog/1.0 (+https://github.com/JUSEONGHONG-CMD/cheongyak-blog)"}
TIMEOUT = 20
TILE = 256
SCALE = 1.6  # 타일 글자가 썸네일에서도 보이게 키운다
ZOOMS = (16, 15, 14, 13)
RED, BLUE = (232, 67, 75), (49, 104, 245)
ATTRIBUTION = "지도: © OpenStreetMap 기여자"
NUMBERS = "①②③④⑤⑥"


@functools.cache
def _tile(z: int, x: int, y: int) -> bytes:
    response = requests.get(TILE_URL.format(z=z, x=x, y=y), headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content


def to_pixel(lat: float, lon: float, z: int) -> tuple[float, float]:
    """위경도 → 줌 z 전체 지도의 픽셀 좌표."""
    size = TILE * 2 ** z
    return (lon + 180) / 360 * size, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * size


def fit_zoom(lat: float, lon: float, others: list[tuple[float, float]], width: int, height: int) -> int:
    """다른 지점이 모두 화면 안쪽에 들어오는 가장 큰 줌."""
    for z in ZOOMS:
        cx, cy = to_pixel(lat, lon, z)
        spots = [to_pixel(*p, z) for p in others]
        if all(abs(x - cx) * SCALE < width * .44 and abs(y - cy) * SCALE < height * .42 for x, y in spots):
            return z
    return ZOOMS[-1]


def _canvas(lat: float, lon: float, z: int, width: int, height: int, center_y: float):
    """(lat, lon) 이 가로 가운데·세로 center_y 에 오는 지도와, 위경도를 화면 좌표로 바꾸는 함수."""
    w, h = width / SCALE, height / SCALE
    px, py = to_pixel(lat, lon, z)
    left, top = px - w / 2, py - h * center_y
    x0, y0 = int(left // TILE), int(top // TILE)
    x1, y1 = int((left + w) // TILE), int((top + h) // TILE)
    sheet = Image.new("RGB", ((x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE))
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            sheet.paste(Image.open(io.BytesIO(_tile(z, x, y))).convert("RGB"), ((x - x0) * TILE, (y - y0) * TILE))
    ox, oy = left - x0 * TILE, top - y0 * TILE
    picture = sheet.crop((int(ox), int(oy), int(ox + w), int(oy + h))).resize((width, height), Image.LANCZOS)

    def place(a: float, b: float) -> tuple[int, int]:
        x, y = to_pixel(a, b, z)
        return int((x - left) * SCALE), int((y - top) * SCALE)
    return picture, place


def _pin(draw, x: int, y: int, size: float = 1.0) -> None:
    """빨간 핀. (x, y) 가 핀 끝이다."""
    r = int(34 * size)
    draw.polygon([(x - r * .8, y - r * 1.5), (x + r * .8, y - r * 1.5), (x, y)], fill=RED)
    draw.ellipse([x - r, y - r * 2.7, x + r, y - r * .7], fill=RED)
    draw.ellipse([x - r * .38, y - r * 2.08, x + r * .38, y - r * 1.32], fill="white")


def _png(picture) -> bytes:
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


def thumbnail_map(lat: float, lon: float) -> bytes | None:
    """썸네일 배경. 글자가 덮는 아래쪽을 피해 핀을 위쪽에 둔다. 받지 못하면 None."""
    try:
        picture, place = _canvas(lat, lon, 15, 1080, 783, 0.36)
    except (requests.RequestException, OSError) as exc:
        print(f"      지도 받기 실패: {type(exc).__name__}")
        return None
    draw = ImageDraw.Draw(picture)
    x, y = place(lat, lon)
    draw.ellipse([x - 88, y - 88, x + 88, y + 88], outline=BLUE, width=7)
    _pin(draw, x, y)
    return _png(picture)


def body_map(lat: float, lon: float, places: list[dict], font: str) -> bytes | None:
    """본문 지도. 단지는 빨간 핀, 주변 시설은 번호. 번호는 글의 목록과 같다."""
    width, height = 1080, 760
    spots = [(p["lat"], p["lon"]) for p in places]
    try:
        picture, place = _canvas(lat, lon, fit_zoom(lat, lon, spots, width, height), width, height, 0.5)
    except (requests.RequestException, OSError) as exc:
        print(f"      지도 받기 실패: {type(exc).__name__}")
        return None
    draw = ImageDraw.Draw(picture)
    label = ImageFont.truetype(font, 34)
    taken: list[tuple[int, int]] = []
    for number, spot in enumerate(spots, 1):
        x, y = place(*spot)
        while any(abs(x - a) < 60 and abs(y - b) < 60 for a, b in taken):  # 가까운 시설끼리 번호가 겹치지 않게
            x += 62
        taken.append((x, y))
        draw.ellipse([x - 28, y - 28, x + 28, y + 28], fill=BLUE, outline="white", width=4)
        draw.text((x, y), str(number), fill="white", font=label, anchor="mm")
    _pin(draw, *place(lat, lon))
    small = ImageFont.truetype(font, 20)
    box = draw.textbbox((0, 0), ATTRIBUTION, font=small)
    draw.rectangle([width - box[2] - 18, height - 36, width, height], fill="white")
    draw.text((width - box[2] - 9, height - 32), ATTRIBUTION, fill=(70, 70, 70), font=small)
    return _png(picture)


def locate(top: dict, kakao_key: str, cache) -> tuple[float, float] | None:
    """대표 공고의 좌표. 분양은 이미 찾아 두었고, LH 임대는 첫 주택 주소로 찾는다."""
    if top.get("coords"):
        return tuple(top["coords"])
    if "deadline" not in top or not kakao_key:
        return None
    for house in top.get("top") or []:
        if house.get("coords"):
            return tuple(house["coords"])
        for address in geo.address_candidates(str(house.get("address") or "").split("(")[0]):
            found = geo.geocode(address, kakao_key, cache)
            if found:
                return tuple(found)
    return None


def nearby(lat: float, lon: float, kakao_key: str, cache) -> list[dict]:
    """가장 가까운 지하철역과 생활 시설. [{'kind', 'name', 'how_far', 'lat', 'lon'}]"""
    out = []
    station = geo.nearest_subway(lat, lon, kakao_key, cache)
    if station:
        out.append({"kind": "지하철역", "name": station["name"], "how_far": f"도보 {station['walk_min']}분",
                    "lat": station["lat"], "lon": station["lon"]})
    for label in geo.AMENITIES:
        spot = geo.nearest_place(lat, lon, label, kakao_key, cache)
        if spot:
            out.append({"kind": label, "name": spot["name"], "how_far": geo._distance(spot["walk_m"]),
                        "lat": spot["lat"], "lon": spot["lon"]})
    return out


def selfcheck() -> None:
    x, y = to_pixel(0, 0, 1)
    assert (round(x), round(y)) == (256, 256)
    near = (37.51, 127.01)
    assert fit_zoom(37.5, 127.0, [near], 1080, 760) < 16  # 1km 넘게 떨어지면 줌을 줄인다
    assert fit_zoom(37.5, 127.0, [], 1080, 760) == 16
    assert fit_zoom(37.5, 127.0, [(40.0, 130.0)], 1080, 760) == 13  # 너무 멀어도 멈춘다
