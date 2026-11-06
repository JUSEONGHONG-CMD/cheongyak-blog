"""썸네일 배경으로 쓸 조감도 찾기.

순서: LH 공고 페이지의 단지조감도 → 분양 사이트의 건물 이미지. 못 찾으면 None 이고 썸네일은 주변 지도로 나간다.
분양 사이트는 robots.txt 가 허용한 곳만, 첫 화면과 안쪽 페이지 몇 개만 본다. 공유 이미지(og:image)는 광고 문구·
전화번호가 들어 있는 경우가 많아 쓰지 않는다(2026-11-06, 24곳 조사). 프로그램이 건물 사진인지 완전히 가려내지는
못하므로 메일에 지도판 썸네일을 함께 첨부한다.
"""

from __future__ import annotations

import io
import re
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import numpy as np
import requests
from PIL import Image

from .notice_pdf import LH_FILE, lh_notice_page

TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; cheongyak-blog/1.0)"}
PAGES_MAX = 5
TRY_MAX = 6
MIN_WIDTH = 800
LH_CREDIT = "CG 이미지 · 자료: LH"
SITE_CREDIT = "CG 이미지 · 자료: 분양 홈페이지"
LH_FILES = re.compile(r"\{cmnAhflSn=[^}]*\}")
IMAGE_WORD = re.compile(r"조감|투시|aerial|bird|perspective|exterior|overview", re.I)
SKIP_WORD = re.compile(r"_bg|logo|icon|map|floor|unit", re.I)
LINK_WORD = re.compile(r"조감|단지\s*(?:안내|배치|정보|개요|소개)|사업\s*개요|브랜드|프리미엄|complex|overview|business|about", re.I)
IMAGE_TAG = re.compile(r"""<img\b[^>]*?(?:data-src|src)=["']([^"']+\.(?:jpe?g|png|webp))[^>]*>""", re.I)
LINK_TAG = re.compile(r"""<a[^>]+href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.S | re.I)


def lh_aerial(page_html: str) -> str | None:
    """LH 공고 페이지 자료 목록에서 단지조감도(코드 15)의 파일 번호."""
    for entry in LH_FILES.findall(page_html):
        fields = dict(x.split("=", 1) for x in entry.strip("{}").split(", ") if "=" in x)
        if fields.get("slPanAhflDsCd") == "15":
            return fields.get("cmnAhflSn")
    return None


def trim_white(image: Image.Image) -> Image.Image:
    """사진 아래에 붙은 흰 표·글 영역을 잘라낸다(분양 사이트의 '사진 + 사업 개요' 이미지)."""
    rows = np.asarray(image.convert("L")).mean(axis=1)
    start = int(len(rows) * .3)
    white = rows > 235
    for y in range(start, len(rows) - 12):
        if white[y:y + 12].all():
            return image.crop((0, 0, image.width, y))
    return image


def looks_like_photo(image: Image.Image) -> bool:
    """가로 사진이고, 너무 어둡거나 색이 없는 배경 그림·흰 바탕이 아닌가."""
    if image.width < MIN_WIDTH or image.width < image.height * 1.1:
        return False
    hsv = np.asarray(image.convert("HSV").resize((64, 64)), dtype=float)
    return 40 < hsv[..., 2].mean() < 235 and hsv[..., 1].mean() > 30


def _bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()


def _robots(url: str) -> urllib.robotparser.RobotFileParser | None:
    host = urlparse(url)
    response = requests.get(f"{host.scheme}://{host.netloc}/robots.txt", headers=HEADERS, timeout=TIMEOUT)
    if response.status_code != 200 or "html" in response.headers.get("content-type", ""):
        return None
    rules = urllib.robotparser.RobotFileParser()
    rules.parse(response.text.splitlines())
    return rules


def site_aerial(home: str) -> bytes | None:
    """분양 사이트에서 건물 이미지 한 장. 수집 금지이거나 못 찾으면 None."""
    rules = _robots(home)
    allowed = lambda url: rules is None or rules.can_fetch(HEADERS["User-Agent"], url)  # noqa: E731
    if not allowed(home):
        return None
    first = requests.get(home, headers=HEADERS, timeout=TIMEOUT)
    pages = [(first.url, first.text)]
    links = []
    for href, text in LINK_TAG.findall(first.text):
        url = urljoin(first.url, href)
        if ((LINK_WORD.search(re.sub(r"<[^>]+>", " ", text)) or LINK_WORD.search(href)) and url not in links
                and urlparse(url).netloc == urlparse(first.url).netloc and allowed(url)):
            links.append(url)
    for url in links[:PAGES_MAX]:
        pages.append((url, requests.get(url, headers=HEADERS, timeout=TIMEOUT).text))
    found = []
    for base, html in pages:
        for match in IMAGE_TAG.finditer(html):
            url = urljoin(base, match.group(1))
            if IMAGE_WORD.search(match.group(0)) and not SKIP_WORD.search(match.group(1)) and url not in found:
                found.append(url)
    for url in [u for u in found if allowed(u)][:TRY_MAX]:
        try:
            image = trim_white(Image.open(io.BytesIO(requests.get(url, headers=HEADERS, timeout=TIMEOUT).content)).convert("RGB"))
        except OSError:
            continue
        if looks_like_photo(image):
            return _bytes(image)
    return None


def find(item: dict, key: str) -> tuple[bytes, str] | None:
    """(이미지, 사진 위 표시 문구). 없으면 None. 어떤 이유로 실패해도 글은 지도로 나간다."""
    home = str(item.get("homepage") or "").strip()
    try:
        if "deadline" in item or "lh.or.kr" in home:
            page = item.get("url") if "deadline" in item else lh_notice_page(item, key)
            number = lh_aerial(requests.get(page, headers=HEADERS, timeout=TIMEOUT).text) if page else None
            if number:
                response = requests.get(LH_FILE.format(number), headers=HEADERS, timeout=60)
                response.raise_for_status()
                return response.content, LH_CREDIT
            return None
        if home.startswith("http"):
            picture = site_aerial(home)
            return (picture, SITE_CREDIT) if picture else None
    except (requests.RequestException, OSError, ValueError) as exc:
        print(f"      조감도 찾기 실패: {type(exc).__name__}")
    return None


def selfcheck() -> None:
    page = ("{cmnAhflSn=1, slPanAhflDsCd=10, cmnAhflNm=토지이용계획도.jpg} "
            "{cmnAhflSn=69120984, slPanAhflDsCd=15, cmnAhflNm=단지조감도2.jpg}")
    assert lh_aerial(page) == "69120984" and lh_aerial("없음") is None
    photo = Image.new("RGB", (1000, 800), (70, 120, 170))
    photo.paste((255, 255, 255), (0, 500, 1000, 800))
    assert trim_white(photo).height == 500 and looks_like_photo(trim_white(photo))
    assert not looks_like_photo(Image.new("RGB", (1000, 600), (10, 10, 10)))  # 어두운 배경 그림
    assert not looks_like_photo(Image.new("RGB", (1000, 600), (130, 130, 130)))  # 색 없는 그림
    assert not looks_like_photo(Image.new("RGB", (600, 900), (70, 120, 170)))  # 세로 사진
    assert IMAGE_WORD.search("pv_exterior.webp") and SKIP_WORD.search("overview_bg.jpg")
