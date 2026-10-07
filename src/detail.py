"""공고 한 건을 항목별로 정리한 본문.

메일 본문과 드라이런 출력이 같은 내용을 보여줘야 하므로, 형식을 한 곳에
모아 둔다. 따로 만들면 한쪽만 고쳐져 내용이 갈라진다.

항목마다 번호를 붙이고, 값이 없어도 번호를 건너뛰지 않는다. 공고마다 같은
번호가 같은 뜻을 가져야 번호만 보고 원하는 정보로 갈 수 있다.
"""

from __future__ import annotations

from datetime import date

from . import strategy

RULE = "━━━━━━━━━━━━━━"

# 1평 = 400/121 ㎡ (약 3.3058). 부동산에서 쓰는 관습 단위다.
SQM_PER_PYEONG = 400 / 121


def pyeong(square_meters: float) -> int:
    """제곱미터를 평으로 바꾼다. 평형은 관습적으로 정수로 말하므로 반올림한다."""
    return round(square_meters / SQM_PER_PYEONG)


def _grouped_areas(areas: list[float]) -> list[str]:
    """같은 평으로 떨어지는 전용면적끼리 묶는다. 84.6·84.7·84.8㎡ → '84.6~84.8㎡(26평)'.

    타입이 많은 단지는 면적을 하나씩 늘어놓으면 카카오톡 한 통 길이를 넘긴다.
    """
    groups: dict[int, list[float]] = {}
    for area in areas:
        groups.setdefault(pyeong(area), []).append(area)
    return [
        (f"{g[0]:g}㎡" if len(g) == 1 else f"{g[0]:g}~{g[-1]:g}㎡") + f"({p}평)"
        for p, g in groups.items()
    ]


def fmt_date(value) -> str:
    return f"{value:%m.%d}" if value else "미정"


def fmt_move_in(value) -> str:
    """입주 예정 시기. API 는 '202610' 같은 연월 문자열로 준다."""
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}년 {int(text[4:])}월"
    return text or "미정"


def location(item: dict) -> str:
    area = (item.get("area_name") or "").strip()
    address = (item.get("address") or "").strip()
    # 공급지역명이 주소에 이미 들어 있으면 중복해서 붙이지 않는다
    if not address:
        return "정보 없음"
    # 택지지구는 '인천 계양구 귤현동, 동양동, …, 경기도 부천시 …' 처럼 동네를
    # 줄줄이 늘어놓아 카카오톡 한 통 길이를 넘긴다. 첫 동네만 적는다.
    if "," in address:
        address = address.split(",")[0].strip() + " 외"
    return address if (not area or area in address) else f"{area} {address}"


def price_span(item: dict) -> str:
    if not item.get("price_min"):
        return ""
    low = item["price_min"] / 100_000_000
    high = item["price_max"] / 100_000_000
    return f"{low:.1f}억" if round(low, 1) == round(high, 1) else f"{low:.1f}~{high:.1f}억"


def title(item: dict) -> str:
    flag = "🔴 마감임박 " if item.get("urgent") else ""
    new = "🆕 " if item.get("is_new") else ""
    rank = f"{item['rank']}. " if item.get("rank") else ""
    return f"{flag}{new}{rank}{item.get('name', '')}"


APPLY_URL = (
    "https://www.applyhome.co.kr/ap/aph/reqst/selectSubscrtReqstAptMainView.do?se={se}&ty={ty}"
)
LH_APPLY_URL = "https://apply.lh.or.kr/lhapply/main.do"


def _within(begin, end, today) -> bool:
    return bool(begin and end and begin <= today <= end)


def apply_link(item: dict, today=None) -> tuple[str, str]:
    """(청약 신청 주소, 접수 상태). 신청할 곳이 없으면 ('', '').

    청약홈은 공고별 신청 주소가 없다. 공고 상세의 '청약신청하기' 버튼도 주택
    구분(se)과 그날 접수 중인 공급 유형(ty: 특별공급 20, 일반공급 10)만 정해
    신청 첫 화면으로 보내고, 단지는 로그인 후 목록에서 고른다. 그 버튼과 같은
    규칙으로 주소를 만든다. 무순위는 청약홈이 늘 20 을, 임의공급은 10 을 쓴다.

    LH 가 공급하는 국민주택(신혼희망타운 등)은 청약홈이 아니라 LH청약플러스에서
    신청한다. LH 공고 상세 페이지(notice_pdf 가 찾아 둔 lh_url)에 신청 버튼이 있다.
    """
    today = today or date.today()
    special =(item.get("special_begin"), item.get("special_end"))
    general = (item.get("general_begin"), item.get("general_end"))
    ends = [d for d in (special[1], general[1]) if d]
    begins = [d for d in (special[0], general[0]) if d]
    if ends and max(ends) < today:
        state = "마감"
    elif _within(*special, today) or _within(*general, today):
        state = "접수 중"
    elif begins and min(begins) > today:
        state = f"{fmt_date(min(begins))}부터"
    else:
        state = ""

    if "한국토지주택공사" in str(item.get("supplier") or ""):
        return item.get("lh_url") or LH_APPLY_URL, state

    se = str((item.get("raw") or {}).get("HOUSE_SECD") or "").strip()
    category = item.get("category")
    if not se:
        return "", ""
    if category == "무순위":
        ty = "20"
    elif category == "임의공급":
        ty = "10"
    elif category == "APT":
        # 일반공급 기간이 아니고 특별공급이 오늘이거나 아직 남았으면 특별공급 화면
        if _within(*general, today):
            ty = "10"
        elif _within(*special, today) or (special[0] and today < special[0]):
            ty = "20"
        else:
            ty = "10"
    else:
        return "", ""
    return APPLY_URL.format(se=se, ty=ty), state


def window_state(end, today=None) -> str:
    """접수 마감까지 남은 날. 이미 지났으면 그렇다고 적는다."""
    today = today or date.today()
    left =(end - today).days
    if left < 0:
        return "  (마감)"
    if left == 0:
        return "  (오늘 마감)"
    return f"  (D-{left})"

def market_lines(item: dict) -> list[str]:
    """⑦ 시세. 분양가가 주변 시세보다 싼지 비싼지를 금액과 말로 함께 적는다.

    부호만 적으면 어느 쪽이 유리한지 읽히지 않는다. 분양은 아직 없는 집을 미리
    사는 것이라 분양가가 시세보다 낮을수록 좋은데, '+5%' 같은 표기로는 그게
    드러나지 않는다.
    """
    m = item.get("market")
    if not m:
        return ["⑦ 시세  주변 실거래 자료 없음"]

    head = (
        f"⑦ 시세  주변 {m['area']:.0f}㎡ 중위 {m['median'] / 1e8:.1f}억"
        f" ({m['cohort']} {m['count']}건, 평균 건축 {m['build_year']}년)"
    )
    lines = [head]

    if "change_pct" in m:
        arrow = "↗" if m["change_pct"] > 0.5 else ("↘" if m["change_pct"] < -0.5 else "→")
        lines.append(
            f"        1년 전 {m['past_median'] / 1e8:.1f}억 → {m['change_pct']:+.1f}% {arrow}"
        )

    if "gap" in m:
        gap = abs(m["gap"]) / 1e8
        if gap < 0.05:
            # 0.1억 미만 차이는 '0.0억 쌈'으로 찍혀 값이 빈 것처럼 보인다
            lines.append(f"        ➖ 분양가가 시세와 비슷함 ({m['gap_pct']:+.0f}%)")
        elif m["gap"] < 0:
            lines.append(f"        ✅ 분양가가 시세보다 {gap:.1f}억 쌈 ({m['gap_pct']:+.0f}%)")
        else:
            lines.append(f"        ⚠️ 분양가가 시세보다 {gap:.1f}억 비쌈 ({m['gap_pct']:+.0f}%)")
        # 비교 대상이 오래된 단지면 신축 프리미엄 때문에 단순 비교가 어렵다
        if m.get("build_year") and m["build_year"] < 2015:
            lines.append("        (비교 대상이 오래된 단지라 신축 프리미엄은 빠져 있음)")
    return lines


def builder_lines(item: dict) -> list[str]:
    """시공사·브랜드·단지 규모. 전체 세대수는 모집공고문에서 읽은 값이다(notice_pdf)."""
    b = item.get("builder_info")
    if not b:
        return []
    parts = []
    if b["builder"]:
        # 공동 시공은 '(주)시티건설, (주)시티종합건설, (주)시티' 처럼 길게 온다
        companies = [c.strip() for c in b["builder"].split(",") if c.strip()]
        who = companies[0] + (f" 외 {len(companies) - 1}곳" if len(companies) > 1 else "")
        who += " (브랜드로 추정)" if b["guessed"] else ""
        label = f"({b['brand']})" if b["brand"] else ""
        tag = f" · 10대 건설사{label}" if b["top10"] else ""
        parts.append(f"시공 {who}{tag}")
    else:
        parts.append("시공사 정보 없음")
    total = item.get("complex_units")
    if total:
        dongs = item.get("complex_dongs")
        scale = f"총 {total:,}세대" + (f"({dongs}개동)" if dongs else "")
        parts.append(scale + (f" 중 이번 {b['units']}세대" if b["units"] else ""))
    elif b["units"]:
        parts.append(f"이번 공급 {b['units']}세대")
    return [f"        🏢 {' · '.join(parts)}"]


def regulation_lines(item: dict) -> list[str]:
    """전매제한·실거주 의무 추정. 투자 계획은 '언제 팔 수 있나'에서 시작한다."""
    if "regulation" not in item:
        return []
    r = item["regulation"]
    if not r:
        return ["        ⚖️ 전매·실거주  공고문 확인 (청약홈이 규제 정보를 주지 않는 공고)"]

    months = r["resale_months"]
    if not months:
        resale = "전매제한 없음"
    else:
        span = f"{months // 12}년" if months % 12 == 0 else f"{months}개월"
        until = r["resale_until"]
        resale = f"전매제한 {span}" + (f"(~{until:%Y.%m.%d})" if until else "(발표일부터)")
    residence = f"실거주 의무 {r['residence']}" if r["residence"] else "실거주 의무 없음"

    lines = []
    if r["zones"]:
        lines.append(f"        ⚖️ {'·'.join(r['zones'])}")
    lines.append(f"        {'⚖️ ' if not r['zones'] else ''}{resale} · {residence}")
    if r["residence"]:
        lines.append("          (입주 후 3년 안에 시작 가능 · 기간은 공고문 확인)")
    return lines


def presale_lines(item: dict) -> list[str]:
    """주변 최근 분양가와의 비교."""
    p = item.get("presales")
    if not p:
        return []
    word = "쌈" if p["gap_pct"] < 0 else "비쌈"
    # 카카오톡 한 통 길이 상한(TEXT_LIMIT)이 빠듯해 이웃 단지 이름은 싣지 않는다
    return [
        f"        🏗 {p['scope']} 최근 2년 {p['kind']}분양 {p['count']}곳"
        f" {p['area']:.0f}㎡ 환산 {p['median'] / 1e8:.1f}억 → {abs(p['gap_pct']):.0f}% {word}",
    ]

def body(item: dict, verdict) -> list[str]:
    """①~⑩ 본문 줄 목록. 제목은 포함하지 않는다."""
    lines: list[str] = []

    lines.append(f"① 위치  {location(item)}")

    # 특별공급과 일반공급은 접수 기간이 다르다. 하나만 보여주면서 다른 쪽의
    # D-day 를 붙이면 '21일 마감인데 D-3' 처럼 앞뒤가 안 맞는다.
    lines.append("② 일정")
    for label, begin, end in (
        ("특공", item.get("special_begin"), item.get("special_end")),
        ("일반", item.get("general_begin"), item.get("general_end")),
    ):
        if not end:
            continue
        lines.append(f"        {label} {fmt_date(begin)} ~ {fmt_date(end)}{window_state(end)}")
    lines.append(f"        발표 {fmt_date(item.get('announce_date'))}")

    lines.append(f"③ 입주  {fmt_move_in(item.get('move_in'))}")
    lines.extend(regulation_lines(item))

    subway = item.get("subway")
    lines.append(
        f"④ 교통  {subway['name']} 도보 {subway['walk_min']}분"
        if subway
        else "④ 교통  지하철역 정보 없음"
    )
    if item.get("nearby"):
        lines.append(f"        {item['nearby']}  (도보 거리 추정)")
    # 대중교통과 자동차를 모두 보여준다. 둘 다 있어야 실제 통근을 가늠할 수 있다.
    shown = False
    for icon, label, key in (("🚌", "대중교통", "commute"), ("🚗", "자동차", "commute_car")):
        parts = [f"{name} {m}분" for name, m in (item.get(key) or {}).items() if m < 999]
        if parts:
            lines.append(f"        {icon} {label} · {' · '.join(parts)}")
            shown = True
    if not shown and item.get("commute") is not None:  # 직장 없는 가상 인물은 통근을 묻지 않는다
        lines.append("        통근시간 조회 실패")

    models = item.get("matched_models") or []
    areas = sorted({round(m["area"], 1) for m in models if m.get("area")})
    lines.append(
        f"⑤ 주택  전용 {', '.join(_grouped_areas(areas))}"
        if areas
        else "⑤ 주택  정보 없음"
    )
    # 흔히 말하는 '34평형'은 전용이 아니라 공급면적 기준이다. 전용만 환산해
    # 보여주면 실제보다 훨씬 작은 집으로 오해하게 된다.
    supplies = sorted({round(m["supply_area"], 1) for m in models if m.get("supply_area")})
    if supplies:
        labels = sorted({pyeong(s) for s in supplies})
        span = f"{supplies[0]:g}㎡" if len(supplies) == 1 else f"{supplies[0]:g}~{supplies[-1]:g}㎡"
        lines.append(f"        공급 {span} → 흔히 {'·'.join(f'{p}평형' for p in labels)}")

    lines.extend(builder_lines(item))

    price = price_span(item)
    if price:
        tag = "  ⚠️ 예산 초과" if item.get("over_budget") else ""
        lines.append(f"        분양가 {price}{tag}")
    else:
        lines.append("        분양가 정보 없음")

    counts = item.get("special_counts") or {}
    lines.append(
        f"⑥ 특공  {' · '.join(f'{k} {v}세대' for k, v in counts.items())}"
        if counts
        else "⑥ 특공  물량 정보 없음"
    )

    lines.extend(market_lines(item))
    lines.extend(presale_lines(item))

    # 부부가 각자 신청할 수 있으므로 두 사람의 순위를 모두 보여준다
    account = getattr(verdict, "account", None)
    accounts = getattr(verdict, "accounts", None) or ([("", account)] if account else [])
    if accounts:
        for index, (label, route) in enumerate(accounts):
            mark = {"가능": "✅", "조건부": "❔"}.get(route.status, "❌")
            head = "⑧ 순위  " if index == 0 else "        "
            who = f"{label}: " if label else ""
            lines.append(f"{head}{mark} {who}{route.reason}")
    else:
        lines.append("⑧ 순위  판정 불가")

    # 신청할 수 있는 '창구'를 나열한다. 이름만으로는 뜻이 안 통하는 제도 용어가
    # 많으므로, 각 창구 아래에 왜 되는지(또는 확인이 필요한지)를 한 줄로 붙인다.
    lines.append("⑨ 신청 가능한 방법")
    for route in (verdict.possible or verdict.conditional):
        mark = "✅" if route.status == "가능" else "❔"
        lines.append(f"    {mark} {route.name}")
        lines.append(f"        └ {route.reason}")
    # 청약순위는 ⑦ 에서 따로 보여주므로 여기서는 빼고 나머지 전제만 싣는다
    for check in verdict.checks:
        if check is not account:
            lines.append(f"    📍 {check.reason}")

    # 번호는 공고마다 같은 뜻이어야 한다. 추천할 내용이 적어도 자리는 지킨다.
    lines.append("⑩ 추천")
    for line in strategy.recommend(item, verdict):
        lines.append(f"    {line}")

    lines.append("⑪ 참고")
    if verdict.marriage_basis:
        lines.append(f"    · {verdict.marriage_basis}")
    lines.append(f"    · 가점 {verdict.gapjeom}점 ({verdict.gapjeom_detail.split(' = ')[0]})")
    if verdict.marriage_advice:
        lines.append(f"    💍 {verdict.marriage_advice}")

    return lines
