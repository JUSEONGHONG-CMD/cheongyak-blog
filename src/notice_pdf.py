"""모집공고문 PDF 에서 단지 전체 세대수를 읽는다.

청약홈 데이터의 공급 세대수는 이번 공고 물량뿐이라, 리모델링·재건축 단지나
무순위 공고에서는 단지 규모를 전혀 알 수 없다. 전체 세대수는 공고문 첫머리
'공급규모' 줄("총 1,149세대 중 일반분양 143세대")에만 있다.

공고문은 청약홈 상세 페이지의 '모집공고문 보기' 링크로 받는다. LH 공고는
공고문이 LH청약플러스에만 있어, LH 공고 목록 API(공공데이터포털
「한국토지주택공사_분양임대공고문 조회 서비스」)로 같은 공고를 찾은 뒤 LH 상세
페이지의 공고문 PDF 를 받는다.

한 번 읽은 결과는 공고가 바뀌지 않으므로 캐시하고, 못 찾은 것(0)도 캐시해
매일 같은 PDF 를 다시 받지 않는다.
"""

from __future__ import annotations

import io
import re
import sys
from datetime import date, timedelta

import requests
from pypdf import PdfReader

TIMEOUT = 60
HEADERS = {"User-Agent": "Mozilla/5.0"}
# 공급규모는 앞쪽(보통 3~7쪽, 유의사항 뒤)에 나온다. 70쪽짜리도 있어 여기까지만 본다.
PAGES = 12

LINK = re.compile(r"""(?is)<a[^>]*href=['"]([^'"]*getAtchmnfl\.do[^'"]*)['"][^>]*>[^<]*공고문""")
TOTAL = re.compile(r"총\s*([\d,]+)\s*세대")
DONG = re.compile(r"(\d+)\s*개\s*동")
SCALE = re.compile(r"공급\s*규모")

LH_LIST = "http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
LH_FILE = "https://apply.lh.or.kr/lhapply/lhFile.do?fileid={}"
LH_PDF = re.compile(r"""fileDownLoad\('(\d+)'\);?["']?\s*>([^<]*공고문[^<]*\.pdf)\s*<""", re.I)


def parse_scale(text: str) -> tuple[int, int]:
    """(전체 세대수, 동 수). 못 찾으면 0.

    '공급규모'가 목차나 제목으로 먼저 나오고 숫자는 다음 줄에 오기도 해서,
    나올 때마다 그 뒤를 조금씩 살핀다.

    PDF 에 따라 글자와 숫자가 따로 뽑혀 '지하 층 지상 층 개동 총 세대 중 … :
    2, 35, 7, 679, 2' 처럼 나오는 일도 있다. 그럴 때는 '총' 앞에 나온 단위
    낱말(층·동) 수만큼 숫자를 건너뛴 자리가 세대수다.
    """
    for match in SCALE.finditer(text):
        window = text[match.start(): match.start() + 300]
        total = TOTAL.search(window)
        if total:
            dong = DONG.search(window)
            return int(total.group(1).replace(",", "")), int(dong.group(1)) if dong else 0

    for match in SCALE.finditer(text):
        line = text[match.start():].split("\n", 1)[0]
        head, sep, _ = line.partition("총")
        numbers = re.findall(r"\d[\d,]*", line.split(":", 1)[-1])
        units = re.findall(r"층|동", head)
        if sep and ":" in line and len(units) < len(numbers):
            dongs = int(numbers[len(units) - 1]) if units and units[-1] == "동" else 0
            return int(numbers[len(units)].replace(",", "")), dongs
    return 0, 0


def _pdf_scale(url: str) -> tuple[int, int] | None:
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return None
    if not response.content.startswith(b"%PDF"):
        return 0, 0
    try:
        reader = PdfReader(io.BytesIO(response.content))
        for page in reader.pages[:PAGES]:
            found = parse_scale(page.extract_text() or "")
            if found[0]:
                return found
    except Exception as exc:  # 깨진 PDF 는 pypdf 가 여러 종류의 예외를 낸다
        print(f"[경고] 모집공고문 읽기 실패: {exc}", file=sys.stderr)
    return 0, 0


def _name_tokens(name: str) -> list[str]:
    """'시흥하중지구 A-4블록' 과 '시흥하중 A4블록' 을 같게 보도록 다듬은 낱말들."""
    cleaned = re.sub(r"[-‐–]", "", (name or "").upper().replace("지구", " "))
    return re.findall(r"[가-힣]{2,}|[A-Z]+\d+", cleaned)


def lh_notice_page(item: dict, service_key: str) -> str | None:
    """같은 공고의 LH청약플러스 상세 페이지 주소. 못 찾으면 빈 문자열, 호출 실패면 None."""
    notice = item.get("notice_date") or date.today()
    params = {
        "serviceKey": service_key, "PG_SZ": 100, "PAGE": 1,
        "PAN_ST_DT": (notice - timedelta(days=7)).strftime("%Y%m%d"),
        "PAN_ED_DT": (notice + timedelta(days=7)).strftime("%Y%m%d"),
    }
    try:
        response = requests.get(LH_LIST, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[경고] LH 공고 목록 조회 실패: {exc}", file=sys.stderr)
        return None
    rows = next((part["dsList"] for part in payload if isinstance(part, dict) and "dsList" in part), [])

    # 이름이 '추가입주자모집' ↔ '잔여세대 추가입주자모집공고' 처럼 조금씩 달라
    # 겹치는 낱말이 가장 많은 공고를 고른다. 다만 '블록·신혼희망타운'만 겹치는
    # 다른 단지를 고르지 않도록 지구명(첫 낱말)과 블록 번호는 꼭 같아야 한다.
    tokens = _name_tokens(item.get("name") or "")
    if not tokens:
        return ""
    wanted = set(tokens)
    required = {tokens[0]} | {t for t in tokens if re.fullmatch(r"[A-Z]+\d+", t)}
    best, score = "", 0
    for row in rows:
        theirs = set(_name_tokens(row.get("PAN_NM") or ""))
        if required <= theirs and len(wanted & theirs) > score:
            best, score = row.get("DTL_URL") or "", len(wanted & theirs)
    return best


def lh_scale(item: dict, service_key: str) -> tuple[int, int] | None:
    page_url = lh_notice_page(item, service_key)
    if not page_url:
        return page_url if page_url is None else (0, 0)
    try:
        page = requests.get(page_url, timeout=TIMEOUT, headers=HEADERS)
        page.raise_for_status()
    except requests.RequestException:
        return None
    match = LH_PDF.search(page.text)
    return _pdf_scale(LH_FILE.format(match.group(1))) if match else (0, 0)


def read_scale(item: dict, service_key: str) -> tuple[int, int] | None:
    """(전체 세대수, 동 수). 공고문이 없거나 못 읽으면 (0, 0).

    네트워크 오류처럼 다음에 다시 해 볼 만한 실패는 None 이다. 캐시하지 않는다.
    """
    detail_url = item.get("url") or ""
    if detail_url.startswith("http"):
        try:
            page = requests.get(detail_url, timeout=TIMEOUT, headers=HEADERS)
            page.raise_for_status()
        except requests.RequestException:
            return None
        match = LINK.search(page.text)
        if match:
            return _pdf_scale(match.group(1).replace("&amp;", "&"))
    if "한국토지주택공사" in str(item.get("supplier") or ""):
        return lh_scale(item, service_key)
    return 0, 0


def enrich(items: list[dict], service_key: str, cache=None) -> None:
    for item in items:
        key = f"scale:{item['house_manage_no']}:{item['pblanc_no']}"
        hit = cache.get(key) if cache is not None else None
        if hit is None:
            result = read_scale(item, service_key)
            hit = list(result or (0, 0))
            if cache is not None and result is not None:
                cache.set(key, hit)
        item["complex_units"], item["complex_dongs"] = hit
        # LH 공고는 LH청약플러스 상세 페이지에서 청약을 신청하므로 그 주소를 남긴다
        if "한국토지주택공사" in str(item.get("supplier") or ""):
            item["lh_url"] = lh_notice_page(item, service_key) or ""


def prune(cache, today: date | None = None) -> int:
    """2년 넘은 공고의 캐시를 버린다. 공고번호 앞 4자리가 연도다."""
    if cache is None:
        return 0
    oldest = (today or date.today()).year - 2
    dropped = 0
    for key in cache.keys():
        if key.startswith("scale:"):
            year = key.split(":")[1][:4]
            if year.isdigit() and int(year) < oldest:
                cache.drop(key)
                dropped += 1
    return dropped
