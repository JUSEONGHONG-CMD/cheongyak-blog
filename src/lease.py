"""LH 매입임대 공고 목록.

매입임대는 LH가 기존 주택을 사서 싸게 빌려주는 임대라 청약홈이 아니라
LH청약플러스에 공고한다. 분양처럼 시세·가점을 따질 일이 없고 자격은
유형마다 소득·자산 기준이 달라, 프로필마다 신청할 수 있는 유형(who_can_apply)의
접수 중인 공고만 목록으로 알린다.

공고 목록은 notice_pdf 가 쓰는 LH 공고 API 와 같은 것이다(주거복지 13, 매입임대 26).

공고 하나에 주택이 수십~수백 호 묶여 있고 주소는 첨부 '주택목록.xlsx' 에만
있다. 임대는 통근시간으로 고르는 경우가 많아, 목록을 읽어 동(주소)별로 묶고
직장까지 가까운 집부터 보여 준다. 직장을 비운 프로필은 보증금 낮은 집부터.

LH 조회와 첨부 파일은 실행당 한 번만 받아 모든 사람이 나눠 쓴다.
"""

from __future__ import annotations

import functools
import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from html import escape

import requests
from pypdf import PdfReader

from . import commute, detail, geo, income_table, lease_rules
from .config import parse_date
from .notice_pdf import LH_FILE, LH_LIST, LH_PDF, TIMEOUT

LH_NOTICES = "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do?mi=1026"
HOUSE_LIST = re.compile(r"fileDownLoad\('(\d+)'\);?[^>]*>([^<]*목록[^<]*\.xlsx)\s*<")
CONTRACT = re.compile(r"계약금\s*[:：]?\s*(?:기본)?임대보증금의\s*(\d+)\s*%")
ANNOUNCE = re.compile(r"(\d{1,2})\.(\d{1,2})\s*\([월화수목금토일]\)\s*별도\s*안내")
# 단지형 공고문: '발표일 : 2026.12.28.(월)' · '[발표일 : ’27.01.06.(수)'. 서류제출대상자 발표가 먼저라 가장 늦은 날을 쓴다.
ANNOUNCE_DATED = re.compile(r"발표일\s*:?\s*[’'‘]?(\d{4}|\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})")
# 공고문 공통 문구: '계약체결 후 90일 이내에 입주를 완료하여야 합니다.'
MOVE_IN_DAYS = 90
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# ponytail: ODsay 무료는 하루 30회라 전 주소를 대중교통으로 조회할 수 없다.
# 자동차 시간(카카오, 한도 넉넉)으로 공고마다 상위 몇 동만 추려 대중교통을 조회한다.
# 한 번 조회한 주소는 캐시에 남으니, 한도를 올리면 이 값만 키우면 된다.
TRANSIT_TOP = 3
NO_ROUTE = commute.UNKNOWN_MINUTES
COMPLEX = {"국민임대", "영구임대", "행복주택", "행복주택(신혼희망)", "통합공공임대", "공공임대(분양전환)"}
# 예전부터 주택목록·배점까지 자세히 다루던 매입임대 3종. 나머지는 lease_rules.judge 로 판정한다.
DETAILED = {"든든전세", "신혼·신생아Ⅱ", "청년"}


@functools.cache
def _lh_rows(service_key: str, today: date) -> tuple[dict, ...]:
    """LH 임대 공고 목록 원본(임대주택 06 · 주거복지 13 · 신혼희망타운 39). 실행당 한 번만 부른다."""
    rows: list[dict] = []
    for upp in ("06", "13", "39"):
        params = {
            "serviceKey": service_key, "PG_SZ": 1000, "PAGE": 1, "UPP_AIS_TP_CD": upp,
            "PAN_ST_DT": (today - timedelta(days=90)).strftime("%Y%m%d"),
            "PAN_ED_DT": today.strftime("%Y%m%d"),
        }
        try:
            response = requests.get(LH_LIST, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[경고] LH 임대 공고 조회 실패({upp}): {exc}", file=sys.stderr)
            continue
        rows += next((p["dsList"] for p in payload if isinstance(p, dict) and "dsList" in p), [])
    return tuple(rows)


def collect(service_key: str, today: date, regions: set[str]) -> list[dict]:
    """접수가 끝나지 않은 LH 임대 공고. regions 는 '서울'·'경기'·'경북' 같은 시·도 줄임말.

    사람마다 부르며, 고쳐 써도 되도록 매번 새 dict 를 만든다.
    """
    rows = _lh_rows(service_key, today)

    # 정정공고는 원래 공고와 이름이 같아 둘 다 잡힌다. 최근 것 하나만 남긴다.
    latest: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("PAN_NM") or "")
        deadline = parse_date(row.get("CLSG_DT"))
        found = lease_rules.classify(str(row.get("AIS_TP_CD_NM") or ""), name)
        where = lease_rules.sido(str(row.get("CNP_CD_NM") or ""))
        if not found or not (where == "전국" or where in regions) or not deadline or deadline < today:
            continue
        key = name.replace("[정정공고]", "").strip()
        if key not in latest or str(row.get("PAN_DT")) > str(latest[key]["notice"]):
            latest[key] = {
                "name": key,
                "kind": found,
                # 단지형(국민·행복·영구·통합·공공임대): 주택목록 대신 페이지 단지 정보 + 공고문 임대조건 표
                "complex": found in COMPLEX,
                "relaxed": "완화" in name,  # 입주자격완화 공고: 소득·자산 요건 배제
                "region": row.get("CNP_CD_NM") or "",
                "status": row.get("PAN_SS") or "",
                "notice": row.get("PAN_DT") or "",
                "deadline": deadline,
                "url": row.get("DTL_URL") or "",
            }
    return sorted(latest.values(), key=lambda x: x["deadline"])


def xlsx_rows(data: bytes) -> list[dict[str, str]]:
    """첫 시트의 행들. {열 문자: 값}. 표 하나 읽자고 openpyxl 을 들이지 않는다."""
    book = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in book.namelist():
        for si in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(XLSX_NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(XLSX_NS + "t")))
    rows = []
    for row in ET.fromstring(book.read("xl/worksheets/sheet1.xml")).iter(XLSX_NS + "row"):
        cells = {}
        for cell in row.iter(XLSX_NS + "c"):
            value = cell.find(XLSX_NS + "v")
            if cell.get("t") == "s" and value is not None:
                text = shared[int(value.text)]
            elif cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(XLSX_NS + "t"))
            else:
                text = value.text if value is not None else ""
            cells[re.match(r"[A-Z]+", cell.get("r")).group()] = (text or "").strip()
        rows.append(cells)
    return rows


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _is_data(row: dict[str, str]) -> bool:
    return row.get("A", "").isdigit() and len(row) >= 5


def _labels(rows: list[dict[str, str]]) -> dict[str, str]:
    """첫 데이터 행 위의 머리글 줄(최대 3줄)을 열마다 이어 붙인 이름.

    공고마다 머리글이 1~3줄이고 묶음 이름은 병합 셀이라 맨 왼쪽 칸에만 있다.
    그래서 빈 칸은 왼쪽 값으로 채워 '신혼2 소득80%초과자 기본임대보증금' 처럼 만든다.
    A열 하나뿐인 줄은 안내문이라 머리글로 치지 않는다.
    """
    first = next((i for i, r in enumerate(rows) if _is_data(r)), len(rows))
    header = []
    for row in reversed(rows[:first]):
        if len(row) >= 2:
            header.insert(0, row)
            if len(header) == 3:
                break
        elif row:
            break
    columns = sorted({k for r in [*header, *rows[first:first + 1]] for k in r}, key=_col)
    labels = {}
    for col in columns:
        parts = []
        for row in header:
            left = [k for k in row if row[k] and _col(k) <= _col(col)]
            if left:
                parts.append(row[max(left, key=_col)])
        labels[col] = re.sub(r"\s", "", " ".join(parts))
    return labels


def _columns(labels: dict[str, str]) -> dict[str, str]:
    """필요한 열 찾기. 보증금·월세는 묶음이 여럿이면 두 가지를 고른다.

    2026-09 공고 주택목록 기준:
    - 든든전세: 임대보증금 한 열뿐(전세).
    - 신혼·신생아Ⅱ(전세형): '소득80%이하'·'소득80%초과' 묶음 → 기본은 '초과',
      '이하' 는 deposit_low 로 따로 둔다(가구 소득이 낮은 프로필에 쓴다, low_income).
    - 청년: '1순위'(수급자 등)·'2·3순위' 와 '최대전환시'·'상한/하한' 변형 → 2·3순위 기본 조건.
    """
    def find(test) -> str:
        return next((k for k, v in labels.items() if test(v)), "")

    deposits = [k for k, v in labels.items()
                if "임대보증금" in v and not re.search("상한|하한|최대전환", v)]

    def score(k: str) -> int:
        return bool(re.search(r"초과|2[·,]?3?순위", labels[k]))

    deposit = max(deposits, key=lambda k: (score(k), -_col(k))) if deposits else ""
    deposit_low = next((k for k in deposits if "이하" in labels[k]), "")

    def rent_of(column: str) -> str:
        # 월세는 보증금 바로 오른쪽 열이다(모든 양식 공통)
        return next((k for k, v in labels.items()
                     if column and _col(k) == _col(column) + 1 and "월임대료" in v), "")

    return {
        # '지번주소'보다 '도로명주소'가 지오코딩이 정확하다
        "address": find(lambda v: v.endswith("도로명주소")) or find(lambda v: v.endswith("주소") and "지번" not in v),
        "name": find(lambda v: "모집단위" in v or v.endswith(("주택군", "주택군이름"))),
        "area": find(lambda v: "전용면적" in v),
        "deposit": deposit,
        "rent": rent_of(deposit),
        "deposit_low": deposit_low,
        "rent_low": rent_of(deposit_low),
    }


def buildings(rows: list[dict[str, str]]) -> list[dict]:
    """주택목록 행 → 주소별 묶음. 공고마다 열 순서가 달라 머리글 이름으로 열을 찾는다."""
    columns = _columns(_labels(rows))
    grouped: dict[str, dict] = {}
    if not columns["address"]:
        return []
    for row in rows:
        if not _is_data(row):
            continue
        address = row.get(columns["address"], "")
        if not address:
            continue
        name = row.get(columns["name"], "")
        if "(" not in name:
            # 인천 양식은 주택군이 '26-7 계양구' 뿐이라 주소 끝의 건물명을 붙인다
            building = address.rsplit(")", 1)[-1].strip() if ")" in address else ""
            name = f"{name.split()[-1]} {building}" if name and building else name or address
        b = grouped.setdefault(address, {
            "address": address, "name": name,
            "units": 0, "areas": [], "deposits": [], "rents": [], "deposits_low": [], "rents_low": [],
        })
        b["units"] += 1
        for field, out in (("area", "areas"), ("deposit", "deposits"), ("rent", "rents"),
                           ("deposit_low", "deposits_low"), ("rent_low", "rents_low")):
            try:
                b[out].append(float(row.get(columns[field], "")))
            except ValueError:
                pass
    return list(grouped.values())


def contract_rate(rows: list[dict[str, str]]) -> float:
    """주택목록에 적힌 계약금 비율('★ 계약금: 기본임대보증금의 10% ★'). 없으면 0."""
    for row in rows:
        for text in row.values():
            match = CONTRACT.search(text)
            if match:
                return int(match.group(1)) / 100
    return 0.0


def announce_date(text: str, deadline: date) -> date | None:
    """공고문 모집일정의 '당첨자·예비입주자 발표'. 표가 '12.2(수) 별도 안내'(발표 → 계약)로 뽑힌다."""
    match = ANNOUNCE.search(text)
    if not match:
        dated = [date(int(y) + (2000 if len(y) == 2 else 0), int(m), int(d)) for y, m, d in ANNOUNCE_DATED.findall(text)]
        dated = [d for d in dated if d >= deadline]
        return max(dated) if dated else None
    month, day = int(match.group(1)), int(match.group(2))
    return date(deadline.year + (month < deadline.month), month, day)


@functools.cache
def _cells(row_html: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
            for c in re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", row_html)]


def complexes(page: str) -> list[dict]:
    """단지형 공고(국민·행복·영구 등) 페이지의 '공급정보' 단지 블록 → 주택목록과 같은 모양의 묶음.

    단지마다 소재지와 주택형 표(전용면적·금회공급 세대수·임대보증금·월임대료)가 있다.
    임대료 칸이 '공고문 확인'인 공고도 있어 그때는 금액 없이 위치·면적만 남는다.
    수급자 전용 주택형은 폼에서 묻지 않으므로 뺀다.
    """
    out = []
    for block in re.split(r'id="wrtancSbdTab\d+"', page)[1:]:
        name = re.search(r'(?s)<h4[^>]*>(.*?)</h4>', block)
        address = re.search(r"소재지\s*:\s*([^<]+)", block)
        table = re.search(r"(?s)<thead>(.*?)</thead>\s*<tbody>(.*?)</tbody>", block)
        if not (name and address and table):
            continue
        head = _cells(table.group(1))
        col = {key: next((i for i, h in enumerate(head) if h.startswith(key)), None)
               for key in ("주택형", "전용면적", "금회공급", "임대보증금", "월임대료")}
        house = {"address": address.group(1).strip(), "name": _cells(f"<td>{name.group(1)}</td>")[0],
                 "units": 0, "areas": [], "deposits": [], "rents": [], "deposits_low": [], "rents_low": []}
        for row in re.findall(r"(?s)<tr>(.*?)</tr>", table.group(2)):
            cells = _cells(row)
            if not cells or "수급자" in cells[0]:
                continue
            num = lambda key: (lambda v: float(v) if v else None)(
                re.sub(r"[^\d.]", "", cells[col[key]]) if col[key] is not None and col[key] < len(cells) else "")
            if num("전용면적"):
                house["areas"].append(num("전용면적"))
            house["units"] += int(num("금회공급") or 0)
            if num("임대보증금"):
                house["deposits"].append(num("임대보증금"))
                house["rents"].append(num("월임대료") or 0)
        if house["areas"]:
            out.append(house)
    return out


def rent_rows(text: str) -> list[tuple[str, float, float, float]]:
    """공고문 임대조건 표의 기본 행 → [(그 행과 앞 3줄, 보증금, 월임대료, 계약금 비율)].

    표 모양은 공고마다 달라도 기본 행은 '보증금 계 · 계약금 · 잔금 · 월임대료' 숫자 넷이 이어지고
    계 = 계약금 + 잔금 이다. 이 등식으로 표의 행만 골라낸다. 전환 행((+)·(-))과 수급자 행은 뺀다.
    """
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if "(+)" in line or "(-" in line or "수급" in line:
            continue
        nums = [float(n.replace(",", "")) for n in re.findall(r"\d{1,3}(?:,\d{3})+", line)]
        for k in range(len(nums) - 3):
            total, down, rest, rent = nums[k: k + 4]
            if total and abs(total - down - rest) <= 10 and rent < total and down < total / 5:
                out.append((" ".join(lines[max(0, i - 3): i + 1]), total, rent, down / total))
                break
    return out


def _attach_rents(houses: list[dict], rows: list[tuple[str, float, float, float]]) -> None:
    """임대조건 행을 단지에 붙인다. 단지가 하나면 전부, 여럿이면 행 앞에 적힌 단지명(첫 낱말)으로."""
    for house in houses:
        if house["deposits"]:
            continue  # 페이지에 이미 금액이 있다
        word = house["name"].split()[0]
        mine = rows if len(houses) == 1 else [r for r in rows if word in r[0]]
        house["deposits"] = [r[1] for r in mine]
        house["rents"] = [r[2] for r in mine]
        if mine and 0.04 <= mine[0][3] <= 0.21:  # 보통 5%·10%·20%. 벗어나면 표를 잘못 읽은 것
            house["contract_rate"] = round(mine[0][3], 3)


def _notice_files(url: str, deadline: date) -> tuple[date | None, tuple[dict, ...]]:
    """(발표일, 주택 묶음들). 공고 페이지·공고문 pdf·주택목록 xlsx 를 실행당 한 번만 받는다."""
    page = requests.get(url, timeout=TIMEOUT).text
    announce = None
    pdf = LH_PDF.search(page)
    reader = None
    if pdf:
        reader = PdfReader(io.BytesIO(requests.get(LH_FILE.format(pdf.group(1)), timeout=60).content))
        announce = announce_date("\n".join(p.extract_text() or "" for p in reader.pages[:4]), deadline)
    match = HOUSE_LIST.search(page)
    if not match:
        # 단지형 공고: 위치·면적은 페이지 단지 정보, 임대료는 페이지에 없으면 공고문 임대조건 표에서
        houses = complexes(page)
        if reader:
            text = "\n".join(p.extract_text() or "" for p in reader.pages[:30])
            _attach_rents(houses, rent_rows(text))
            announce = announce or announce_date(text, deadline)  # 단지형은 일정표가 4쪽 뒤에 있다
        return announce, tuple(houses)
    rows = xlsx_rows(requests.get(LH_FILE.format(match.group(1)), timeout=60).content)
    rate = contract_rate(rows)
    return announce, tuple({**b, "contract_rate": rate} for b in buildings(rows))


def read_notice(lease: dict) -> list[dict]:
    """공고 첨부에서 주택 묶음을 읽는다. 발표일은 lease 에 적는다. 사람마다 고쳐 쓰도록 복사본."""
    try:
        lease["announce"], found = _notice_files(lease["url"], lease["deadline"])
    except Exception as exc:  # 깨진 PDF·xlsx 는 여러 종류의 예외를 낸다
        print(f"[경고] 공고 첨부 읽기 실패 ({lease['name'][:20]}): {exc}", file=sys.stderr)
        return []
    return [dict(b) for b in found]


def _rank_key(b: dict) -> tuple[int, int]:
    """대중교통 합 → 자동차 합. 대중교통을 못 구한 집은 뒤로 보낸다."""
    transit, car = b.get("commute") or {}, b.get("commute_car") or {}
    usable = transit and all(m < NO_ROUTE for m in transit.values())
    return (sum(transit.values()) if usable else NO_ROUTE * 2, sum(car.values()) or NO_ROUTE * 2)


def add_commute(leases: list[dict], profile: dict, kakao_key: str, odsay_key: str, cache) -> None:
    """공고마다 주택목록을 읽고 통근 가까운 집 TOP 을 붙인 뒤, 공고도 그 순서로 정렬한다.

    직장을 비운 프로필은 통근 대신 보증금 낮은 집을 보여 주고, 공고는 마감 순이 된다.
    """
    destinations = commute.workplace_coords(profile, kakao_key, cache) if kakao_key else {}
    warning = home_warning(profile)
    for lease in leases:
        if lease["base_kind"] in DETAILED:
            lease["track"] = track(lease["base_kind"], lease["who"], profile)
            # 든든전세·(혼인신고 후)신혼Ⅱ 는 세대 전체가 무주택이어야 한다. 청년·예비신혼은 본인 기준.
            if lease_rules.separated_home(profile.get("applicant") or {}) and (
                lease["base_kind"] == "든든전세"
                or (lease["base_kind"] == "신혼·신생아Ⅱ" and not lease_rules.facts(profile, date.today())["engaged"])
            ):
                lease["track"] += " · ❔ 세대분리 필요"
        lease["warning"] = warning
        houses = read_notice(lease)  # 단지형은 페이지 단지 정보 + 공고문 임대조건
        if lease["base_kind"] == "신혼·신생아Ⅱ" and low_income(profile):
            for b in houses:
                if b["deposits_low"]:
                    b["deposits"], b["rents"] = b["deposits_low"], b["rents_low"]
        lease["houses"] = sum(b["units"] for b in houses)
        if not destinations:
            lease["top"] = sorted(houses, key=lambda b: min(b["deposits"] or [9e12]))[:TRANSIT_TOP]
            continue
        found = []
        for b in houses:
            # LH 주소는 '도로명 21-4(고색동) 건물명' 꼴이라 괄호 앞이 가장 정확하다
            candidates = geo.address_candidates(b["address"].split("(")[0])
            coords = next((c for c in (geo.geocode(a, kakao_key, cache) for a in candidates) if c), None)
            if coords and destinations:
                b["commute_car"] = {n: commute.car_minutes(coords, d, kakao_key, cache) for n, d in destinations.items()}
                b["coords"] = coords
                found.append(b)
        found.sort(key=lambda b: sum(b["commute_car"].values()))
        for b in found[:TRANSIT_TOP]:
            if odsay_key:
                b["commute"] = {n: commute.transit_minutes(b["coords"], d, odsay_key, cache) for n, d in destinations.items()}
        lease["top"] = sorted(found[:TRANSIT_TOP], key=_rank_key)
        for b in lease["top"]:
            b["nearby"] = geo.nearby_text(b["coords"], kakao_key, cache)
    leases.sort(key=lambda x: (_rank_key(x["top"][0]) if x.get("top") else (NO_ROUTE * 9, 0), x["deadline"]))


def _minutes(values: dict) -> str:
    return "·".join(str(m) if m < NO_ROUTE else "?" for m in values.values()) if values else "-"


def _money(won: float) -> str:
    return f"{won / 1e8:.2f}억" if won >= 1e8 else f"{won / 1e4:,.0f}만"


def _won(values: list[float]) -> str:
    if not values:
        return ""
    low, high = _money(min(values)), _money(max(values))
    if low == high:
        return low
    # '1.83억~1.89억' → '1.83~1.89억'. 단위가 다르면 둘 다 남긴다.
    return f"{low[:-1]}~{high}" if low[-1] == high[-1] else f"{low}~{high}"


def _house_line(b: dict) -> tuple[str, str]:
    """(이름·통근, 보증금·면적·호수)."""
    car = b.get("commute_car")
    commute_text = f"  🚌{_minutes(b.get('commute') or {})} 🚗{_minutes(car)}분" if car else ""
    area = ""
    if b["areas"]:
        low, high = min(b["areas"]), max(b["areas"])
        if detail.pyeong(low) == detail.pyeong(high):
            area = f"전용 {low:.0f}㎡({detail.pyeong(low)}평)"
        else:
            area = f"전용 {low:.0f}~{high:.0f}㎡({detail.pyeong(low)}~{detail.pyeong(high)}평)"
    cost = f"보증금 {_won(b['deposits'])}" if b["deposits"] else "임대료는 공고문 확인"
    if cost and b.get("contract_rate"):
        cost += f"(계약금 {_won([d * b['contract_rate'] for d in b['deposits']])})"
    if cost and any(b.get("rents") or []):
        cost += f" + 월세 {_won(b['rents'])}"
    facts = " · ".join(x for x in (cost, area, f"{b['units']}호" if b["units"] else "") if x)
    return f"{b['name'][:22]}{commute_text}", facts


def move_in(lease: dict) -> str:
    """'발표 12.02 → 계약 후 90일 안 입주'. 계약일은 공고에 '별도 안내'라 발표일까지만 확정이다."""
    announce = lease.get("announce")
    head = f"발표 {announce:%m.%d} → " if announce else ""
    return f"{head}계약 후 {MOVE_IN_DAYS}일 안 입주"


# 초기 비용 = 보증금 전액. 공고문상 잔금은 입주 때 내고, 계약금 비율은 공고에 없어 계약 때 안내받는다.
# 든든전세는 전세(월세 없음), 신혼·신생아Ⅱ는 보증금 80% 준전세, 청년은 보증금 소액 + 월세.
COST_NOTE = ("초기 비용=보증금 전액(잔금은 입주 때), 월세·관리비는 매달 · "
             "보증금↔월세 일부 전환 가능 · 계약금은 목록에 비율 있을 때만 표시")


def _workplaces(leases: list[dict]) -> str:
    for lease in leases:
        for b in lease.get("top") or []:
            return "·".join(b.get("commute_car") or {})
    return ""


# 2026-09 LH 공고문 소득 기준표(도시근로자 가구원수별 월평균소득, 1인 +20%p·2인 +10%p 가산 반영).
# 신혼·신생아Ⅱ: 130% 이하, 맞벌이는 200% 이하. 청년 3순위: 본인 1인 가구 100% 이하.
NEWLYWED_CAP = {
    130: {1: 5_720_045, 2: 8_212_778, 3: 10_618_958, 4: 11_442_863, 5: 12_125_081, 6: 12_878_142},
    200: {1: 8_389_399, 2: 12_319_167, 3: 16_336_858, 4: 17_604_404, 5: 18_653_970, 6: 19_812_526},
}
YOUTH_INCOME_LIMIT = 4_576_036


def _people(profile: dict) -> list[tuple[str, dict]]:
    """(표시 이름, 사람) — 본인과, 입력돼 있으면 배우자(예정자)."""
    return [
        (person.get("label") or default, person)
        for key, default in (("applicant", "본인"), ("spouse", "배우자"))
        if (person := profile.get(key))
    ]


def _age(person: dict, today: date) -> int | None:
    birth = parse_date(person.get("birth_date"))
    if birth:
        return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return int(person["age"]) if person.get("age") else None


def who_can_apply(base_kind: str, profile: dict, today: date | None = None) -> list[str]:
    """이 유형에 넣을 수 있는 사람의 표시 이름들. 빈 목록이면 이 프로필에는 안 보낸다.

    모르는 값(null)은 탈락시키지 않고 통과시킨다. 판정은 참고용이고 최종 확인은 공고문이다.
    """
    today = today or date.today()
    household = profile.get("household") or {}
    people = _people(profile)
    if not people:
        return []
    if base_kind == "든든전세":
        # 소득·자산 기준 없음. 무주택세대구성원이면 된다.
        return [] if people[0][1].get("homeless") is False else [people[0][0]]
    if base_kind == "신혼·신생아Ⅱ":
        # 신혼(혼인 7년 이내)·예비신혼부부. 배우자(예정자)가 없으면 해당 없음.
        if not (household.get("marriage_registered") or profile.get("spouse")):
            return []
        incomes = [income_table.person_monthly(person) for _, person in people]
        size = min(max(int(household.get("household_size") or 2), 1), 6)
        cap = NEWLYWED_CAP[200 if sum(1 for i in incomes if i) >= 2 else 130][size]
        return [people[0][0]] if income_table.household_income(profile) <= cap else []
    if base_kind == "청년":
        # 미혼 19~39세, 본인 소득 1인 가구 100% 이하. 부모가 집이 있어도 신청 가능.
        if household.get("marriage_registered"):
            return []
        return [
            label for label, person in people
            if (_age(person, today) is None or 19 <= _age(person, today) <= 39)
            and income_table.person_monthly(person) <= YOUTH_INCOME_LIMIT
        ]
    return []


# 신혼·신생아Ⅱ '소득 80% 이하' 묶음 판정용 도시근로자 가구원수별 월평균소득 100%(가산 전).
# ponytail: 가산(1인 +20%p·2인 +10%p)을 빼고 엄격하게 본다. 경계선 가구는 비싼
# '초과' 조건이 보여 비용을 과하게 잡을 뿐 놓치지는 않는다. 정확히 하려면 가산 반영.
INCOME_100 = {1: 3_813_363, 2: 5_866_270, 3: 8_168_429, 4: 8_802_202, 5: 9_326_985, 6: 10_181_384}


def low_income(profile: dict) -> bool:
    """가구 소득이 도시근로자 월평균소득 80% 이하인지 (신혼·신생아Ⅱ 저소득 묶음)."""
    household = profile.get("household") or {}
    size = min(max(int(household.get("household_size") or 2), 1), 6)
    total = income_table.household_income(profile)
    return 0 < total <= INCOME_100[size] * 0.8


def for_profile(leases: list[dict], profile: dict, today: date | None = None) -> list[dict]:
    """이 프로필이 넣을 수 있는 공고만 남기고, 누가 넣는지(who)와 표시 이름(kind)을 붙인다."""
    kept = []
    for lease in leases:
        base = lease["kind"]
        if base in DETAILED:
            who = who_can_apply(base, profile, today)
            if who:
                label = f"{base}({'·'.join(who)})" if base == "청년" else base
                kept.append({**lease, "base_kind": base, "kind": label, "who": who})
            continue
        tracks = lease_rules.judge(base, profile, today or date.today(), lease.get("relaxed", False))
        if tracks:
            text = " / ".join(f"{t} {'✅' if s == lease_rules.YES else '❔'}" for t, s in tracks)
            kept.append({**lease, "base_kind": base, "who": ["본인"], "track": text,
                         "sure": all(s == lease_rules.YES for _, s in tracks)})
    return kept


def _account_points(count: int) -> int:
    """청약통장 납입 횟수 배점(세 유형 공통): 24회↑ 3 · 12회↑ 2 · 6회↑ 1."""
    return 3 if count >= 24 else 2 if count >= 12 else 1 if count >= 6 else 0


def track(base_kind: str, who: list[str], profile: dict) -> str:
    """이 공고에 어떤 전형·순위로 넣고 배점이 몇 점인지. 2026-09 LH 공고문 기준.

    - 든든전세: 순위 없음. 신생아 2점 + 자녀 수(1인 1·2인 2·3인↑ 3점), 동점은 추첨.
    - 신혼·신생아Ⅱ: 1순위 신생아 · 2순위 자녀 있는 (예비)신혼 · 3순위 자녀 없는 (예비)신혼.
      배점은 자녀 수(1인 2·2인 3·3인↑ 4점) + 신청자 통장 + 그 시·군 연속 거주기간(1~3점).
    - 청년: 미혼 단독. 2순위 본인+부모 소득 100% 이하 · 3순위 본인 1인 100% 이하.
      배점은 부모 무주택 2점 + 통장 등.
    """
    household = profile.get("household") or {}
    children = int(household.get("children") or 0)
    people = dict(_people(profile))
    first = who[0] if who else "본인"
    if base_kind == "든든전세":
        return f"{first} 신청 · 순위 구분 없음 · 배점 {min(children, 3)}점 → 동점 추첨"
    if base_kind == "신혼·신생아Ⅱ":
        kind_of = "신혼부부" if household.get("marriage_registered") else "예비신혼부부"
        rank = "2순위(신생아면 1순위)" if children else "3순위"
        count = (people.get(first, {}).get("account") or {}).get("payment_count")
        account = _account_points(int(count or 0))
        points = account + (min(children, 3) + 1 if children else 0)
        return f"{kind_of} {rank} · 배점 {points}점(통장 {account}) + 거주기간(그 시 거주 시 1~3)"
    if base_kind == "청년":
        parts = []
        for label in who:
            person = people.get(label, {})
            account = _account_points(int((person.get("account") or {}).get("payment_count") or 0))
            parents = {True: "부모 무주택 +2", False: "부모 무주택 가점 없음"}.get(
                person.get("parents_homeless"), "부모 무주택이면 +2")
            parts.append(f"{label} 단독 3순위(부모 소득 합산 100% 이하면 2순위) · 배점 {account}점(통장) · {parents}")
        return " / ".join(parts)
    return ""


def home_warning(profile: dict) -> str:
    """혼인신고 후 무주택세대구성원 판정 위험. 세 유형 모두 '입주 시까지 무주택 유지'가 조건이다.

    공고문 무주택세대구성원 표: 배우자의 직계존속은 '세대 분리된 신청자 배우자의 주민등록표등본에
    등재된 사람'이면 포함된다. 그래서 혼인신고 뒤 배우자 등본에 남은 세대원 주택이 우리 주택이 된다.
    """
    notes = []
    mine = lease_rules.separated_home(profile.get("applicant") or {})
    if mine:
        notes.append(f"⚠ 본인 등본(세대)에 {mine.get('owner') or '세대원'} 명의 집이 있어 무주택세대구성원이 아님 → "
                     "청년·예비신혼 유형은 그대로 가능, 나머지(❔ 세대분리 필요)는 공고일 전 세대분리해야 신청")
    home = (profile.get("spouse") or {}).get("household_home") or {}
    if home and not home.get("separated"):
        notes.append(f"⚠ 혼인신고 시 배우자 등본의 {home.get('owner') or '세대원'} 주택으로 무주택 탈락 → "
                     "신혼Ⅱ·든든전세는 신고 전 배우자 주소 분리, 청년은 미혼일 때만")
    return " / ".join(notes)


def _dday(deadline: date, today: date) -> str:
    left = (deadline - today).days
    return "오늘 마감" if left == 0 else f"D-{left}"


def html_section(leases: list[dict], today: date) -> str:
    th = "padding:6px 10px;border-bottom:2px solid #ccc;font-size:12px;text-align:left;color:#555"
    td = "padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;vertical-align:top"
    small = "color:#888;font-size:11px"

    def houses(x: dict) -> str:
        return "".join(
            f"<div style='margin-bottom:4px'>{rank}) {escape(head)}<br><span style='{small}'>"
            f"{escape(facts)}</span>"
            + (f"<br><span style='{small}'>{escape(b['nearby'])}</span>" if b.get("nearby") else "")
            + "</div>"
            for rank, b in enumerate(x.get("top") or [], 1)
            for head, facts in [_house_line(b)]
        ) or (f"<span style='{small}'>단지형 공고 — 단지 위치·면적·임대료는 공고문에서 확인하세요.</span>"
              if x.get("complex") else
              f"<span style='{small}'>주택목록을 읽지 못했습니다. 공고에서 확인하세요.</span>")

    rows = "".join(
        f"<tr><td style='{td}'><b>{escape(x['kind'])}</b><br>"
        f"<span style='{small}'>{escape(x.get('track') or '')}</span></td>"
        f"<td style='{td}'><a href='{escape(x['url'])}' style='color:#06c;text-decoration:none'>"
        f"{escape(x['name'])}</a><br><span style='{small}'>{escape(x['region'])}"
        f"{' · %d호' % x['houses'] if x.get('houses') else ''}</span></td>"
        f"<td style='{td}'>{houses(x)}</td>"
        f"<td style='{td};white-space:nowrap'>{x['deadline']:%m.%d}<br>"
        f"<span style='{small}'>{_dday(x['deadline'], today)}</span></td>"
        f"<td style='{td}'>{escape(move_in(x))}</td></tr>"
        for x in leases
    ) or f"<tr><td colspan='5' style='{td}'>접수 중인 공고가 없습니다.</td></tr>"
    workplaces = _workplaces(leases)
    order = "통근 가까운 순" if workplaces else "마감 순, 보증금 낮은 집부터"
    houses_head = (f"통근 가까운 집 (🚌대중교통 🚗자동차, {escape(workplaces)} 분)"
                   if workplaces else "보증금 낮은 집")
    return (
        "<h2 style='margin:28px 0 4px'>🏘 LH 임대 (전세·월세)</h2>"
        "<p style='margin:0 0 10px;color:#666;font-size:13px'>LH · 접수 중 "
        f"{len(leases)}건 · {order} · LH 임대 전 유형 중 신청 가능(✅)·확인 필요(❔)한 것만. "
        "총자산은 예금 등 금융자산까지 합한 기준이니 공고문으로 확인하세요</p>"
        "<table width='100%' style='border-collapse:collapse;width:100%;margin-bottom:24px'><tr>"
        f"<th width='22%' style='{th}'>전형 · 신청 순위</th><th width='26%' style='{th}'>공고</th>"
        f"<th width='30%' style='{th}'>{houses_head}</th>"
        f"<th width='9%' style='{th}'>마감</th><th width='13%' style='{th}'>입주 시기</th></tr>"
        f"{rows}</table>"
        f"<p style='margin:-16px 0 24px;color:#666;font-size:12px'>💰 {escape(COST_NOTE)}</p>"
        + (f"<p style='margin:-16px 0 24px;color:#b45309;font-size:12px'>{escape(leases[0]['warning'])}</p>"
           if leases and leases[0].get("warning") else "")
    )


if __name__ == "__main__":
    assert lease_rules.classify("매입임대", "[인천]26년 7차 신혼·신생아Ⅱ(전세형) 예비입주자 모집공고") == "신혼·신생아Ⅱ"
    assert lease_rules.classify("매입임대", "[대전충남] 신혼·신생아  II (전세형) 매입임대주택") == "신혼·신생아Ⅱ"
    assert lease_rules.classify("매입임대", "[서울지역본부] 26년 3차 신혼·신생아 매입임대Ⅰ 예비입주자") == "신혼·신생아Ⅰ"
    assert lease_rules.classify("매입임대", "[경기남부] 든든전세 매입임대주택 예비입주자 모집공고") == "든든전세"
    assert lease_rules.classify("매입임대", "[서울] 26년 3차 청년 매입임대주택") == "청년"
    assert lease_rules.classify("매입임대", "기숙사형 청년주택 입주자 모집공고") == "청년"
    assert lease_rules.classify("매입임대", "[인천지역본부] 26년 2차 고령자 매입임대주택") == "고령자 매입임대"

    # 머리글이 두 줄이고 열 순서가 다른 목록도 이름으로 찾는다
    rows = [
        {"A": "든든전세주택 공급대상주택 목록"},
        {"A": "순번", "B": "공급주택"},
        {"B": "시도", "D": "모집단위(주택군)", "E": "주소", "K": "전용\n면적", "P": "임대보증금"},
        {"A": "1", "B": "경기도", "D": "수원고색(디케이타워)", "E": "수원시 호매실로 21-4(고색동)", "K": "74.47", "P": "183251000"},
        {"A": "2", "B": "경기도", "D": "수원고색(디케이타워)", "E": "수원시 호매실로 21-4(고색동)", "K": "74.47", "P": "189388000"},
        {"A": "3", "B": "경기도", "D": "안양(가나)", "E": "안양시 만안로 1", "K": "59", "P": "x"},
    ]
    found = buildings(rows)
    assert [(b["name"], b["units"]) for b in found] == [("수원고색(디케이타워)", 2), ("안양(가나)", 1)]
    assert _won(found[0]["deposits"]) == "1.83~1.89억" and found[1]["deposits"] == []
    near = {"commute": {"A역": 30, "B역": 40}, "commute_car": {"A역": 20, "B역": 20}}
    far = {"commute": {"A역": 50, "B역": 40}, "commute_car": {"A역": 10, "B역": 10}}
    broken = {"commute": {"A역": NO_ROUTE, "B역": 10}, "commute_car": {"A역": 5, "B역": 5}}
    assert sorted([broken, far, near], key=_rank_key) == [near, far, broken]

    # 공고문 모집일정 표가 뽑힌 모양 그대로 (경기남부 2026-09-30 공고)
    schedule = "10.30(금)\n~\n12.1(화)\n12.2(수) 별도 안내\n * 상기 일정은"
    assert announce_date(schedule, date(2026, 10, 23)) == date(2026, 12, 2)
    assert announce_date("1.5(화) 별도 안내", date(2026, 12, 10)) == date(2027, 1, 5)
    assert move_in({"announce": date(2026, 12, 2)}) == "발표 12.02 → 계약 후 90일 안 입주"
    assert _house_line({**near, "name": "a", "units": 1, "areas": [], "deposits": [2e8], "rents": [150000.0]})[1] \
        == "보증금 2.00억 + 월세 15만 · 1호"
    # 직장 미입력이면 통근 칸 없이 이름만
    assert _house_line({"name": "a", "units": 1, "areas": [], "deposits": [2e8], "rents": []})[0] == "a"

    # 단지형 공고 임대조건 표: 계 = 계약금 + 잔금 인 행만, 전환 행은 뺀다 (원주태장4·인천석남 공고문 그대로)
    table = ("원주태장4 51 25,874,000 1,293,700 24,580,300 211,070\n(+) 24,000,000 49,874,000 91,070\n"
             "21A\n대학생·\n청년(소득無) 32,708,000 1,635,000 31,073,000 166,260\n"
             "주거급여수급자 9,000,000 450,000 8,550,000 50,000")
    assert [(r[1], r[2]) for r in rent_rows(table)] == [(25_874_000, 211_070), (32_708_000, 166_260)]
    assert announce_date("ㅇ 발표일 : ’26.10.15.(목) 14:00\n■ 당첨자 발표 [발표일 : ’27.01.06.(수)", date(2026, 10, 7)) == date(2027, 1, 6)
    assert announce_date("• 발표일 : 2026. 12. 2.(수) 이후", date(2026, 10, 7)) == date(2026, 12, 2)
    two = [{"name": "원주태장4", "deposits": [], "rents": []}, {"name": "원주무실", "deposits": [], "rents": []}]
    _attach_rents(two, rent_rows(table))
    assert two[0]["deposits"] == [25_874_000] and two[1]["deposits"] == []

    # 신혼·신생아Ⅱ: 소득 80% 초과 묶음 (서울 2026-09-17 공고 주택목록 그대로)
    newlywed = [
        {"A": "순번", "B": "지자체", "E": "주택정보", "Q": "신혼·신생아2(전세형) \n소득80%이하", "S": "신혼·신생아2(전세형) \n소득80%초과"},
        {},
        {"B": "지역본부", "E": "주소", "H": "주택군", "J": "전용\n면적", "Q": "임대보증금", "R": "월임대료", "S": "임대보증금", "T": "월임대료"},
        {"A": "1", "E": "서울 강남구 언주로69길 23-4(역삼동)", "H": "강남역삼동(엔트라움)", "J": "29.27",
         "Q": "285163000", "R": "319620", "S": "325900000", "T": "357820"},
    ]
    b = buildings(newlywed)[0]
    assert (b["name"], b["deposits"], b["rents"]) == ("강남역삼동(엔트라움)", [325900000.0], [357820.0])
    assert (b["deposits_low"], b["rents_low"]) == ([285163000.0], [319620.0])
    assert _house_line({**b, **near})[1] == "보증금 3.26억 + 월세 36만 · 전용 29㎡(9평) · 1호"

    # 청년: 2·3순위 기본 임대조건 (최대전환시 열은 고르지 않는다)
    youth = [
        {"A": "순번", "B": "지자체", "E": "주택정보", "S": "청년1순위", "W": "청년 2순위, 3순위"},
        {"S": "기본 임대조건", "U": "임대료→보증금 최대전환시", "W": "기본 임대조건", "Y": "임대료→보증금 최대전환시"},
        {"E": "주소", "H": "주택군 이름", "S": "임대보증금", "T": "월임대료", "U": "임대보증금", "V": "월임대료",
         "W": "임대보증금", "X": "월임대료", "Y": "임대보증금", "Z": "월임대료"},
        {"A": "1", "E": "서울 강남구 논현로12길 23-5", "H": "강남개포동(T&K개포)", "S": "1000000", "T": "555650",
         "U": "67600000", "V": "222650", "W": "2000000", "X": "682040", "Y": "83800000", "Z": "273040"},
    ]
    b = buildings(youth)[0]
    assert (b["deposits"], b["rents"]) == ([2000000.0], [682040.0])
    assert _house_line({**b, **near})[1] == "보증금 200만 + 월세 68만 · 1호"

    # 인천 양식: 머리글 3줄, 도로명/지번 주소, 기본·하한·상한 변형 (2026-09-17 신혼Ⅱ 주택목록)
    incheon = [
        {"A": "★ 계약금: 기본임대보증금의 10% ★"},
        {},
        {"A": "연번", "B": "모집단위(주택군)", "C": "소재지", "I": "주택정보", "P": "임대조건"},
        {"C": "광역지자체", "E": "도로명주소", "F": "지번주소", "I": "전용면적",
         "P": "신혼2(전세형) 소득 80%이하자", "T": "신혼2(전세형) 소득 80%초과자"},
        {"P": "기본임대보증금", "Q": "기본월임대료", "R": "하한임대보증금", "S": "상한월임대료",
         "T": "기본임대보증금", "U": "기본월임대료", "V": "하한임대보증금", "W": "상한월임대료"},
        {"A": "1", "B": "26-7 계양구", "C": "인천광역시", "E": "인천광역시 계양구 아나지로 398(작전동) 예린펠리시티",
         "F": "작전동 682-3", "I": "67.43", "P": "91434000", "Q": "123550", "R": "9234000", "S": "363300",
         "T": "104497000", "U": "138240", "V": "10497000", "W": "412400"},
    ]
    b = buildings(incheon)[0]
    assert (b["name"], b["deposits"], b["rents"]) == ("계양구 예린펠리시티", [104497000.0], [138240.0])
    assert contract_rate(incheon) == 0.1 and contract_rate(newlywed) == 0
    b = {**b, **near, "contract_rate": 0.1}
    assert _house_line(b)[1] == "보증금 1.04억(계약금 1,045만) + 월세 14만 · 전용 67㎡(20평) · 1호"
    assert _house_line({**b, "areas": [59.9, 84.9]})[1].endswith("전용 60~85㎡(18~26평) · 1호")

    # 누가 어떤 유형에 넣을 수 있나 (가짜 프로필)
    today = date(2026, 10, 5)
    single = {"applicant": {"birth_date": "1996-05-01", "monthly_income": 3_000_000}}
    rich_single = {"applicant": {"age": 30, "monthly_income": 6_000_000}}
    couple = {
        "household": {"marriage_registered": False, "children": 0, "household_size": 2},
        "applicant": {"monthly_income": 6_000_000, "account": {"payment_count": 100}},
        "spouse": {"monthly_income": 3_000_000, "account": {"payment_count": 30}, "parents_homeless": False,
                   "household_home": {"owner": "부모", "separated": False}},
    }
    assert who_can_apply("든든전세", single, today) == ["본인"]
    assert who_can_apply("든든전세", {"applicant": {"homeless": False}}, today) == []
    assert who_can_apply("신혼·신생아Ⅱ", single, today) == []          # 배우자 없음
    assert who_can_apply("신혼·신생아Ⅱ", couple, today) == ["본인"]     # 맞벌이 900만 ≤ 2인 200%
    assert who_can_apply("신혼·신생아Ⅱ", {**couple, "spouse": {"monthly_income": 7_000_000}}, today) == []
    assert who_can_apply("청년", single, today) == ["본인"]
    assert who_can_apply("청년", rich_single, today) == []
    assert who_can_apply("청년", couple, today) == ["배우자"]
    assert who_can_apply("청년", {**couple, "household": {"marriage_registered": True}}, today) == []
    assert who_can_apply("청년", {"applicant": {"age": 45}}, today) == []

    leases = [{"kind": "청년", "name": "x"}, {"kind": "신혼·신생아Ⅱ", "name": "y"}]
    assert [x["kind"] for x in for_profile(leases, couple, today)] == ["청년(배우자)", "신혼·신생아Ⅱ"]
    assert [x["kind"] for x in for_profile(leases, single, today)] == ["청년(본인)"]

    assert track("든든전세", ["본인"], couple) == "본인 신청 · 순위 구분 없음 · 배점 0점 → 동점 추첨"
    assert track("신혼·신생아Ⅱ", ["본인"], couple).startswith("예비신혼부부 3순위 · 배점 3점(통장 3)")
    assert track("청년", ["배우자"], couple).startswith("배우자 단독 3순위")
    assert "부모 무주택 가점 없음" in track("청년", ["배우자"], couple)
    assert home_warning(couple).startswith("⚠ 혼인신고 시 배우자 등본의 부모 주택")
    later = {**couple, "household": {"marriage_registered": True, "children": 1}}
    assert track("신혼·신생아Ⅱ", ["본인"], later).startswith("신혼부부 2순위(신생아면 1순위) · 배점 5점")
    assert home_warning(single) == ""
    # 소득 80% 이하 묶음: 2인 가산 전 100% 586.6만의 80% = 469.3만
    assert not low_income(couple)
    assert low_income({**couple, "applicant": {"monthly_income": 4_000_000}, "spouse": {"monthly_income": 0}})
    assert not low_income({"applicant": {}})  # 소득 모르면 기본(초과) 조건
    print("ok")
