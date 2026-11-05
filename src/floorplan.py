"""대표 공고의 평면도.

LH 공고는 공고 페이지에 팸플릿 PDF가 붙어 있고, 글자가 살아 있는 팸플릿은 쪽마다 주택형(46A1 등)과
평면도 그림이 따로 들어 있다(남양주왕숙 A-2, 2026-11-05 확인). 그 쪽에는 입체 그림(투명 배경 RGBA)과
방 이름이 적힌 평면도(RGB)가 함께 있어 RGB 쪽을 고른다.

쪽 전체가 통이미지인 팸플릿(안산신길2 A-6)은 주택형을 알 수 없어 넣지 않는다. 다른 주택형 도면이
붙으면 틀린 글이 되기 때문이다. 민간 분양 사이트는 수집을 막거나(robots.txt) 구조가 제각각이라 받지 않는다.
못 가져온 공고는 item['plan_link'] 로 공식 사이트로 가는 링크를 단다.
"""

from __future__ import annotations

import io
import re

import requests
from pypdf import PdfReader

from .notice_pdf import HEADERS, LH_FILE, lh_notice_page

TIMEOUT = 90
PLANS_MAX = 2  # 글에 넣는 면적 수
MIN_SIDE = 300  # 가로·세로가 이보다 작은 그림은 치수 띠·옵션 도면·아이콘이다
PAMPHLET = re.compile(r"""fileDownLoad\('(\d+)'\);?["']?\s*>([^<]*팸플릿[^<]*\.pdf)\s*<""", re.I)


def page_label(text: str, area: int) -> str:
    """이 쪽이 전용 area㎡의 평면도 쪽이면 주택형 이름('46A1'), 아니면 ''."""
    if "평면도" not in text:
        return ""
    found = re.search(rf"(?<![\d.]){area}[A-Z]\d?(?![\dA-Za-z])", text)
    return found.group(0) if found else ""


def pick_plan(page) -> bytes | None:
    """쪽에서 평면도 그림(투명 배경이 아닌 가장 큰 그림)을 PNG로."""
    best = None
    for picture in page.images:
        image = picture.image
        if image.mode != "RGBA" and min(image.size) >= MIN_SIDE and (
                best is None or image.width * image.height > best.width * best.height):
            best = image
    if best is None:
        return None
    out = io.BytesIO()
    best.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def from_pamphlet(data: bytes, areas: list[int]) -> list[tuple[str, bytes]]:
    """팸플릿에서 면적마다 첫 평면도 쪽의 (주택형, PNG)."""
    reader = PdfReader(io.BytesIO(data))
    texts = [page.extract_text() or "" for page in reader.pages]
    found = []
    for area in areas:
        for page, text in zip(reader.pages, texts):
            label = page_label(text, area)
            png = pick_plan(page) if label else None
            if png:
                found.append((label, png))
                break
    return found


def enrich(item: dict, key: str, images: dict) -> None:
    """item['floor_plans'] = [(cid, 주택형)], 못 가져오면 item['plan_link'] 에 평면도를 볼 수 있는 공식 주소."""
    item["floor_plans"], item["plan_link"] = [], ""
    home = str(item.get("homepage") or "").strip()
    if "lh.or.kr" not in home:
        item["plan_link"] = home if home.startswith("http") else ""
        return
    areas = list(dict.fromkeys(int(m["area"]) for m in item.get("matched_models") or [] if m.get("area")))[:PLANS_MAX]
    try:
        page_url = lh_notice_page(item, key) or ""
        item["plan_link"] = page_url
        if not page_url or not areas:
            return
        page = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT).text
        found = PAMPHLET.search(page)
        if not found:
            return
        response = requests.get(LH_FILE.format(found.group(1)), headers=HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        plans = from_pamphlet(response.content, areas)
    except Exception as exc:  # 깨진 PDF 는 pypdf 가 여러 종류의 예외를 낸다. 글은 링크만 달고 나간다
        print(f"      평면도 가져오기 실패: {type(exc).__name__}")
        return
    for number, (label, png) in enumerate(plans):
        images[f"plan{number}@blog"] = png
        item["floor_plans"].append((f"plan{number}@blog", label))
    if plans:
        item["plan_link"] = ""


def selfcheck() -> None:
    assert page_label("※ 본 평면도 및 아이소는 개략도 46A1 46A2", 46) == "46A1"
    assert page_label("※ 본 평면도 55D형 55D 주택", 55) == "55D"
    assert page_label("남양주왕숙 총 608세대 46A1 446A2", 46) == ""  # '평면도' 쪽이 아니면 아니다
    assert page_label("본 평면도 146A1 주택", 46) == ""
    assert page_label("본 평면도 55A1", 46) == ""
    item = {"homepage": "http://www.example.com"}
    enrich(item, "", {})
    assert item["plan_link"] == "http://www.example.com" and item["floor_plans"] == []
    item = {"homepage": ""}
    enrich(item, "", {})
    assert item["plan_link"] == ""
