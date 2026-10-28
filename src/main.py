"""청약 블로그 원고 생성기 진입점.

가상 인물(profiles/*.yaml)마다 지원 가능한 분양·LH 임대 공고를 골라 네이버 블로그에
붙여 넣을 원고를 만들고, 운영자에게 메일로 보낸다(인물 한 명 = 글 한 편 = 메일 한 통).
지난 원고 뒤로 새로 뜬 공고가 없는 인물은 그날 건너뛴다.

판정·수집 코드는 청약 알리미 베타에서 복사해 왔다. 베타와 따로 운용한다.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from datetime import date
from html import escape
from pathlib import Path

from . import (
    applyhome,
    builder,
    detail,
    eligibility,
    geo,
    lease,
    market,
    notice_pdf,
    notify_email,
    presale,
    ranking,
    regulation,
    thumbnail,
    validate,
)
from .config import MissingKey, env, load_profiles
from . import Redacted  # 키 가리기는 패키지를 불러올 때 이미 켜진다(src/__init__.py)
from .state import SeenStore

SLOTS = ("08:30", "12:30", "19:30")  # 그날 나가는 순서대로 붙는 예약 발행 시각
DETAIL_MAX = 6  # 글 하나에 자세히 쓰는 새 공고 수. 나머지는 한 줄 목록
# 누구나 넣는 유형. 인물마다 다를 게 없어 상세 자리는 특공·일반공급 공고에 먼저 준다
ANYONE = ("임의공급", "무순위", "오피스텔등", "공공지원민간임대")


def applyhome_key(item: dict) -> str:
    return f"{item.get('category')}:{item.get('house_manage_no')}:{item.get('pblanc_no')}"


def split_detail(sale_new: list, lease_new: list, limit: int = DETAIL_MAX) -> tuple[list, list]:
    """자세히 쓸 새 공고를 분양·임대 반씩, 한쪽이 모자라면 다른 쪽이 채운다."""
    half = limit // 2
    sale_n = min(len(sale_new), max(half, limit - len(lease_new)))
    lease_n = min(len(lease_new), limit - sale_n)
    return sale_new[:sale_n], lease_new[:lease_n]


def collect_sale(profile, keys, cache, today):
    """베타 run_sale 의 수집·판정 부분. (지원 가능 공고 순위순, 판정, 조회 불완전 목록)"""
    items, degraded = applyhome.collect(profile, keys["service"], today)
    verdicts = {}
    eligible = []
    for item in items:
        verdict = eligibility.evaluate(item, profile, today)
        verdicts[applyhome_key(item)] = verdict
        if verdict.eligible:
            eligible.append(item)
    if keys["kakao"]:
        geo.enrich(eligible, keys["kakao"], cache)
        market.enrich(eligible, keys["service"], keys["kakao"], cache, today)
        market.prune(cache, today)
    presale.enrich(eligible, keys["service"], cache, today)
    presale.prune(cache, today)
    regulation.enrich(eligible)
    builder.enrich(eligible)
    notice_pdf.enrich(eligible, keys["service"], cache)
    notice_pdf.prune(cache, today)
    return ranking.rank(eligible, today), verdicts, degraded


def collect_lease(profile, keys, cache, today):
    filters = profile.get("filters") or {}
    regions = {*(filters.get("regions") or []), *(filters.get("city_regions") or [])}
    leases = lease.for_profile(lease.collect(keys["service"], today, regions), profile, today)
    lease.add_commute(leases, profile, keys["kakao"], "", cache)  # 직장이 없어 보증금 낮은 집 순
    for x in leases:
        x["seen_key"] = f"lease:{x['name']}"
    return leases


def body_of(html: str) -> str:
    """메일용 완성 문서에서 <body> 안쪽만."""
    return html.split("<body", 1)[1].split(">", 1)[1].rsplit("</body>", 1)[0]


def one_liner(item: dict, today: date) -> str:
    """'계속 접수 중' 목록 한 줄."""
    if "deadline" in item:  # LH 임대
        left = (item["deadline"] - today).days
        return (f"<li>[LH {escape(item['kind'])}] <a href='{escape(item['url'])}'>{escape(item['name'])}</a>"
                f" · 마감 {item['deadline']:%m.%d} (D-{left})</li>")
    left = item.get("days_left")
    facts = [x for x in (detail.areas_text(item), detail.price_span(item)) if x]
    return (f"<li>[{escape(detail.kind_word(item))}] "
            f"<a href='{escape((item.get('url') or '').strip())}'>{escape(str(item.get('name') or ''))}</a>"
            + "".join(f" · {escape(f)}" for f in facts)
            + (f" · D-{left}" if left is not None else "") + "</li>")


OPENINGS = (
    "{m}월 {d}일, {label} 조건으로 새로 넣을 수 있는 공고가 {n}건 나왔어요.",
    "{label} 기준으로 이번에 새로 열린 공고를 확인해 보니 {n}건이 있었어요.",
    "{label} 상황이라면 눈여겨볼 새 공고가 {n}건이에요 ({m}월 {d}일 기준).",
)


def dday(item: dict) -> str:
    """분양은 days_left, LH 임대는 deadline 으로 'D-9'."""
    left = item.get("days_left") if "deadline" not in item else (item["deadline"] - date.today()).days
    return "" if left is None else ("오늘 마감" if left == 0 else f"D-{left} 마감")


def intro_html(alias: str, blog: dict, sale_show, lease_show, verdicts, count: int, today: date,
               short: bool = False) -> str:
    """원고마다 달라지는 도입부 2~4문장. 같은 틀이 반복되면 네이버가 유사 문서로 볼 수 있어
    여는 문장을 인물·날짜로 돌리고, 나머지는 그날 공고 숫자로 채운다."""
    variant = (sum(map(ord, alias)) + today.toordinal()) % len(OPENINGS)
    lines = [OPENINGS[variant].format(m=today.month, d=today.day, label=blog["label"], n=count)]
    if sale_show:  # 글 맨 앞 문장이 단지명으로 시작하게 아래에서 맨 앞으로 옮긴다
        top = sale_show[0]
        route = best_route(verdicts.get(applyhome_key(top)))
        where = " · ".join(x for x in (str(top.get("area_name") or ""), dday(top)) if x)
        top_name = plain_name(top["name"])
        line = f"가장 먼저 볼 곳은 <b>{escape(top_name)}</b>({escape(where)}){validate.josa(top_name, '으로')}"
        line += f", {escape(route)} 자격으로 넣을 수 있어요." if route else " 신청할 수 있어요."
        if short:  # 읽기용 본문은 바로 아래 '핵심 3줄 요약'이 자격·분양가를 말한다
            line = (f"이번에 가장 먼저 볼 곳은 <b>{escape(top_name)}</b>"
                    f"{'이에요' if validate.josa(top_name, '이') == '이' else '예요'}.")
        m = top.get("market") or {}
        if m.get("gap_pct") is not None and top.get("price_min") and not short:
            side = "저렴해요" if m["gap_pct"] < 0 else "비싸요"
            price = detail.price_span(top)
            line += (f" 분양가는 {price}{validate.josa(price, '으로')} 주변 시세보다 약 {abs(m['gap_pct']):.0f}% "
                     f"{side}.")
        lines.insert(0, line)
        special = sum(1 for x in sale_show if "특공" in best_route(verdicts.get(applyhome_key(x))))
        if special > 1:
            lines.append(f"특별공급으로 넣을 수 있는 분양 공고는 {special}곳이에요.")
    if lease_show:
        kinds = list(dict.fromkeys(x["kind"].split("(")[0] for x in lease_show))
        first = min(x["deadline"] for x in lease_show)
        lines.append(f"LH 임대는 {escape('·'.join(kinds[:3]))} 등 {len(lease_show)}건이 새로 접수 중이고, "
                     f"가장 빠른 마감은 {first.month}월 {first.day}일이에요.")
    if count > len(sale_show) + len(lease_show):
        lines.append("나머지 공고는 아래 자세히 보기의 목록에 한 줄씩 정리했어요.")
    return f"<p style='font-size:15px;line-height:1.8'>{' '.join(lines)}</p>"


def lease_title(name: str) -> str:
    """LH 공고명을 카드에 맞게 줄인다: '[서울지역본부] 26년 2차 비분양전환형 든든전세주택 입주자 모집공고'
    → '26년 2차 비분양전환형 든든전세주택'."""
    name = re.sub(r"^\s*\[[^\]]*\]\s*", "", str(name))
    return re.sub(r"\s*(추가|예비)?\s*(입주자|입주자격완화)?\s*(선착순\s*)?모집\s*(공고문?)?.*$", "", name).strip() or name


def days_left(item: dict, today: date) -> int:
    if "deadline" in item:  # LH 임대
        return (item["deadline"] - today).days
    return item["days_left"] if item.get("days_left") is not None else 999


# 썸네일 체크리스트: 대표 공고의 신청 경로마다 이 인물이 채운 조건 세 가지.
# 판정 코드(eligibility·lease_rules)가 보는 요건을 그대로 옮겼다. 경로 이름은 notify_email.short_route 결과.
CHECKS = {
    "신혼부부 특공": ("무주택 세대구성원", "혼인 7년 이내", "소득 기준 충족"),
    "생애최초 특공": ("집을 가진 적 없음", "소득세 5년 이상 납부", "소득 기준 충족"),
    "신생아 특공": ("2세 미만 자녀", "무주택 세대구성원", "소득·자산 기준 확인"),
    "다자녀 특공": ("미성년 자녀 2명 이상", "무주택 세대구성원", "청약통장 6개월 이상"),
    "노부모부양 특공": ("65세 이상 부모 3년 부양", "무주택 세대주", "청약통장 1순위"),
    "일반공급": ("청약통장 1순위", "무주택 세대구성원", "가점·추첨으로 선정"),
    "무순위 (줍줍)": ("청약통장 없이 신청", "무주택 세대구성원", "만 19세 이상"),
    "임의공급": ("청약통장 없이 신청", "거주지 제한 없음", "만 19세 이상"),
    "든든전세": ("무주택 세대구성원", "소득·자산 기준 없음", "동점이면 추첨"),
    "청년": ("만 19~39세 미혼", "본인 무주택", "소득 기준 충족"),
    "신혼·신생아Ⅱ": ("혼인 7년 이내·예비신혼", "무주택 세대구성원", "소득 기준 충족"),
}
LOOSE_CHECKS = ("청약통장 없이 신청", "거주지 제한 없음", "만 19세 이상")  # 오피스텔·도시형 등
LEASE_CHECKS = ("무주택 세대구성원", "소득·자산 기준 충족", "자세한 기준은 공고문")


def family_icons(profile: dict) -> list[str]:
    """썸네일 가족 아이콘. blog.icons 가 있으면 그대로, 없으면 부부는 아내·남편, 아이는 딸·아들 번갈아(최대 2명)."""
    if profile["blog"].get("icons"):
        return list(profile["blog"]["icons"])
    household = profile.get("household") or {}
    kids = int(household.get("minor_children", household.get("children")) or 0)
    adults = ["woman", "man"] if profile.get("spouse") else ["man"]
    return adults + ["girl", "boy", "girl"][:min(kids, 3)]  # 다자녀(3명)와 두 자녀를 구분해 3명까지


def card_text(alias: str, profile: dict, sale_show, lease_show, verdicts, today: date) -> dict:
    """썸네일 카드 인자. 대표는 제목·도입부와 같은 공고(상세 첫 공고)."""
    soonest = (sale_show or lease_show)[0]
    if "deadline" in soonest:  # LH 임대
        kind = soonest["base_kind"] if soonest.get("base_kind") in CHECKS else soonest["kind"].split("(")[0]
        checks = CHECKS.get(kind, LEASE_CHECKS)
        sure = soonest.get("sure", True)  # 상세 유형(든든전세·청년·신혼Ⅱ)은 who 로 이미 걸렀다
        name = lease_title(soonest["name"])
        facts = " · ".join(x for x in (soonest["kind"].split("(")[0], soonest.get("region"),
                                       f"{soonest['houses']}호" if soonest.get("houses") else "") if x)
    else:
        verdict = verdicts.get(applyhome_key(soonest))
        route = best_route(verdict).replace("❔", "")
        checks = CHECKS.get(route, LOOSE_CHECKS)
        sure = bool(verdict and verdict.possible)
        name = plain_name(soonest["name"])
        kind = detail.kind_word(soonest).split("(")[0].replace(" 물량", "")
        facts = " · ".join(x for x in (kind, detail.areas_text(soonest), detail.price_span(soonest)) if x)
    left = days_left(soonest, today)
    number = int(re.sub(r"\D", "", alias.split("-")[0]) or 0)
    return {
        "model_no": number, "label": profile["blog"]["label"], "date_line": f"{today.month}월 {today.day}일 기준",
        "checks": list(checks), "stamp": "신청 가능" if sure else "확인 필요", "name": name,
        "verb": "지원할 수 있어요" if sure else "확인해 보세요", "facts": facts,
        "dday": f"D-{left}" if left < 999 else "",
        "icons": family_icons(profile),
        "senior": any((p.get("birth_date") or "9999") < f"{today.year - 60}"
                      for p in (profile.get("applicant") or {}, profile.get("spouse") or {})),
    }


# ── 읽기용 본문 ──────────────────────────────────────────
# 참고 블로그(월급구조대, 2026-10-28 분석)의 틀: 인사·도입 → 📌 핵심 3줄 요약(인용구) → 기준일 → 질문형 소제목
# 섹션 → ❓ 자주 묻는 질문 → 마무리. 서식(가운데 정렬·인용구·hr·글자 크기·2~3칸 표)은 Gmail→네이버 붙여넣기
# 시험(2026-10-28)에서 모두 살아났다. 지금까지 보내던 정보는 그 아래 '📂 자세히 보기'에 그대로 둔다.
BLOG_NAME = "부동산&청약 공부 기록장"
BODY = "font-size:17px;line-height:1.8"
HEAD = "font-size:22px;font-weight:bold;margin:28px 0 12px"
SMALL = "font-size:13px;color:#888;line-height:1.7"
HR = "<hr style='border:none;border-top:1px solid #ccc;margin:28px 0'>"
QUOTE = f"border-left:4px solid #3168f5;background:#f5f8ff;margin:16px 0;padding:12px 18px;{BODY}"
CELL = "border:1px solid #ddd;padding:6px 12px;text-align:center"


def _table(head: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """2~3칸 간단한 표. 칸이 적고 글이 짧으면 네이버에 붙여도 너비가 깨지지 않는다(시험 E1·E2)."""
    th = "".join(f"<th style='{CELL};background:#f3f5f8'>{escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td style='{CELL}'>{escape(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table style='border-collapse:collapse;margin:12px auto;font-size:15px'><tr>{th}</tr>{body}</table>"


def _type(name) -> str:
    """청약홈 주택형 '084.3522A' → '84A'."""
    found = re.match(r"0*(\d+)(?:\.\d+)?\s*([A-Za-z]*)", str(name or "").strip())
    return f"{found.group(1)}{found.group(2).upper()}" if found else (str(name or "").strip() or "-")


def _money(won: float) -> str:
    """5_300_0000 → '5,300만 원', 1.2억 이상 → '1.2억'."""
    return f"{won / 1e8:.1f}억" if won >= 1e8 else f"{round(won / 1e4):,}만 원"


def _md(d) -> str:
    return f"{d.month}월 {d.day}일" if d else "미정"


def _span(begin, end) -> str:
    if begin and end and begin != end:
        return f"{_md(begin)}~{_md(end)}"
    return _md(end or begin)


def _route_sentence(route, label: str) -> str:
    """판정 한 줄을 사람 말로: '신혼부부 특공으로 넣을 수 있어요. (가구 소득 94% · 기준 100% 이하 충족)'."""
    name = notify_email.short_route(route.name)
    who = re.search(r"([가-힣]+) 명의\)", route.name)
    owner = f"{who.group(1)} 명의로 " if who else ""
    if route.status == "가능":
        text = f"<b>{escape(name)}</b>{validate.josa(name, '으로')} {owner}넣을 수 있어요."
    else:
        text = f"<b>{escape(name)}</b>도 {owner}가능성이 있지만, 공고문으로 확인이 필요해요."
    return text + (f" <span style='color:#666'>({escape(route.reason)})</span>" if route.reason else "")


def readable_html(profile: dict, sale_show, lease_show, verdicts, count: int, intro: str, today: date) -> str:
    """원고 앞쪽 읽기용 본문. 대표 공고 하나를 중심으로 질문형 섹션을 만든다."""
    blog = profile["blog"]
    label = blog["label"]
    top = (sale_show or lease_show)[0]
    is_lease = "deadline" in top
    name = lease_title(top["name"]) if is_lease else plain_name(top["name"])
    eun = validate.josa(name, "은")
    out = [f"<p style='text-align:center;{BODY}'>안녕하세요! 조건별로 넣을 수 있는 청약을 골라 드리는 "
           f"<b>{BLOG_NAME}</b>입니다. 🏠</p>",
           re.sub(r"<p style='[^']*'>", f"<p style='{BODY}'>", intro, count=1)]

    # 📌 핵심 3줄 요약
    if is_lease:
        houses = top.get("top") or []
        deposits = [d for b in houses for d in (b.get("deposits") or [])]
        lines = [f"① {escape(name)}{eun} <b>{_md(top['deadline'])}</b>까지 접수해요 ({dday(top)}).",
                 f"② {escape(label)} 조건이면 <b>{escape(top['kind'].split('(')[0])}</b> 유형으로 신청할 수 있어요.",
                 f"③ {escape(str(top.get('region') or ''))} 일대 "
                 + (f"{top['houses']}호를 모집하고, " if top.get("houses") else "")
                 + (f"보증금은 <b>{lease._won([min(deposits)])}</b>부터예요." if deposits else "보증금은 공고문에서 확인해요.")]
    else:
        verdict = verdicts.get(applyhome_key(top))
        route = best_route(verdict).replace("❔", "")
        begins = [d for d in (top.get("special_begin"), top.get("general_begin")) if d]
        ends = [d for d in (top.get("special_end"), top.get("general_end")) if d]
        m = top.get("market") or {}
        price = detail.price_span(top)
        third = ", ".join(x for x in (detail.areas_text(top), f"분양가 <b>{price}</b>" if price else "") if x)
        if m.get("gap_pct") is not None and price:
            third += f"으로 주변 시세보다 약 {abs(m['gap_pct']):.0f}% {'저렴해요' if m['gap_pct'] < 0 else '비싸요'}."
        else:
            third += "예요." if third else "분양가는 공고문에서 확인해요."
        lines = [f"① {escape(name)}{eun} <b>{_span(min(begins, default=None), max(ends, default=None))}</b> "
                 f"접수해요 ({dday(top)}).",
                 f"② {escape(label)} 조건이면 <b>{escape(route or '청약')}</b>{validate.josa(route or '청약', '으로')} "
                 + ("넣을 수 있어요." if verdict and verdict.possible else "넣을 수 있는지 공고문 확인이 필요해요."),
                 f"③ {third}"]
    out.append(f"<blockquote style='{QUOTE}'><b>📌 핵심 3줄 요약</b><br>{'<br>'.join(lines)}</blockquote>")
    out.append(f"<p style='{SMALL}'>※ 이 글은 {today.year}년 {today.month}월 {today.day}일 청약홈·LH 공고를 기준으로 "
               "정리했어요. 신청 전 모집공고문으로 한 번 더 확인해 주세요.</p>")

    # 📅 일정
    out.append(HR + f"<p style='{HEAD}'>📅 {escape(name)}, 언제 접수하나요?</p>")
    if is_lease:
        rows = [("접수 마감", _md(top["deadline"])), ("당첨자 발표", _md(top.get("announce"))),
                ("입주", "계약 후 90일 안")]
        out.append(f"<p style='{BODY}'>접수는 <b>{_md(top['deadline'])}</b>에 끝나요. LH 임대는 발표 뒤 계약을 거쳐 "
                   "입주하니, 보증금 마련 시점을 미리 계산해 두세요.</p>")
    else:
        rows = []
        if top.get("special_end"):
            rows.append(("특별공급 접수", _span(top.get("special_begin"), top.get("special_end"))))
        if top.get("general_end"):
            rows.append(("일반공급 접수", _span(top.get("general_begin"), top.get("general_end"))))
        rows += [("당첨자 발표", _md(top.get("announce_date"))), ("입주 예정", detail.fmt_move_in(top.get("move_in")))]
        out.append(f"<p style='{BODY}'>마감은 <b>{_md(max(ends, default=None))}</b>이고, 당첨자 발표는 "
                   f"<b>{_md(top.get('announce_date'))}</b>이에요. 발표 뒤 계약까지 시간이 짧으니 계약금을 미리 "
                   "준비해 두세요.</p>")
    out.append(_table(("구분", "일정"), rows))

    # ✅ 자격
    out.append(HR + f"<p style='{HEAD}'>✅ {escape(label)}도 넣을 수 있을까?</p>")
    if is_lease:
        out.append(f"<p style='{BODY}'><b>{escape(top['kind'])}</b> 유형으로 신청할 수 있어요."
                   + (f" <span style='color:#666'>({escape(top['track'])})</span>" if top.get("track") else "") + "</p>")
    else:
        routes = (verdict.possible + verdict.conditional) if verdict else []
        seen_names, said = set(), []
        for r in routes:
            short = notify_email.short_route(r.name)
            if short not in seen_names:
                seen_names.add(short)
                said.append(f"<p style='{BODY};margin:6px 0'>• {_route_sentence(r, label)}</p>")
        out += said[:4] or [f"<p style='{BODY}'>신청 방법은 아래 자세히 보기에서 확인해 주세요.</p>"]
    checks = card_text_checks(top, verdicts)
    if checks:
        out.append(f"<p style='{BODY}'>신청 전에 이 세 가지를 확인하세요: "
                   + " · ".join(f"<b>{escape(c)}</b>" for c in checks) + ".</p>")

    # 💰 금액
    if is_lease:
        houses = top.get("top") or []
        if houses:
            out.append(HR + f"<p style='{HEAD}'>💰 보증금과 월세는 얼마인가요?</p>")
            out.append(_table(("주택", "조건"), [(b["name"][:24], lease._house_line(b)[1]) for b in houses[:5]]))
    else:
        models = [x for x in (top.get("matched_models") or []) if x.get("area")][:6]
        if models:
            out.append(HR + f"<p style='{HEAD}'>💰 {escape(name)} 분양가와 평수는?</p>")
            out.append(_table(("주택형", "전용(평)", "분양가"),
                              [(_type(x.get("type_name")),
                                f"{x['area']:.0f}㎡({detail.pyeong(x['area'])}평)",
                                f"{x['price'] / 1e8:.1f}억" if x.get("price") else "공고문 확인") for x in models]))
            if m.get("median"):
                out.append(f"<p style='{BODY}'>주변 비슷한 면적 아파트의 최근 실거래 중위값은 <b>{m['median'] / 1e8:.1f}억</b>이에요. "
                           "자세한 시세 추이는 아래 그래프를 참고하세요.</p>")

    # 🔎 함께 볼 공고
    others = [x for x in (*sale_show, *lease_show) if x is not top]
    if others:
        out.append(HR + f"<p style='{HEAD}'>🔎 이번에 함께 볼 공고</p>")
        for x in others:
            if "deadline" in x:
                text = (f"{lease_title(x['name'])} — {x['kind'].split('(')[0]}, {x.get('region') or ''}, "
                        f"{_md(x['deadline'])} 마감")
            else:
                text = detail.summary_sentence(x)
            out.append(f"<p style='{BODY};margin:6px 0'>• {escape(text)}</p>")

    # ❓ 자주 묻는 질문
    faq = []
    if is_lease:
        faq.append(("청약통장이 꼭 있어야 하나요?", "LH 임대는 유형마다 달라요. 통장이 필요 없는 유형도 있고, 가점에 쓰이는 "
                    "유형도 있으니 공고문의 신청 자격을 확인하세요."))
        faq.append(("계약금은 얼마인가요?", "보증금의 일부를 계약 때 내고 나머지는 입주할 때 내요. 비율은 공고마다 달라요."))
    else:
        if top.get("category") in ANYONE:
            faq.append(("청약통장이 꼭 있어야 하나요?", f"아니요. {detail.kind_word(top)}{validate.josa(detail.kind_word(top), '은')} "
                        "청약통장 없이 신청할 수 있어요."))
        elif verdict and verdict.account:
            faq.append(("청약통장 순위는 어떻게 되나요?", f"{verdict.account.reason}."))
        if top.get("price_min"):
            low = top["price_min"] * 0.1 / 1e8
            faq.append(("계약금은 얼마나 준비해야 하나요?", f"공고마다 다르지만 보통 분양가의 10~20%예요. "
                        f"이 단지는 최소 약 {_money(low * 1e8)} 이상을 계약 때 내야 할 수 있어요."))
    faq.append(("마감 뒤에도 넣을 수 있나요?", "아니요. 마감 뒤에는 신청할 수 없어요. 미계약분이 생기면 무순위로 다시 "
                "나올 수 있으니 이 블로그에서 다시 알려 드릴게요."))
    out.append(HR + f"<p style='{HEAD}'>❓ 자주 묻는 질문</p>")
    out += [f"<p style='{BODY}'><b>Q. {escape(q)}</b><br>A. {escape(a)}</p>" for q, a in faq[:3]]

    out.append(HR + f"<p style='{BODY}'>{escape(label)} 조건이라면 <b>{escape(name)}</b>부터 확인해 보세요. "
               "아래 <b>📂 자세히 보기</b>에 공고별 상세 정보와 시세 그래프를 정리했어요. "
               "도움이 되셨다면 저장해 두고 신청 전에 다시 확인해 보세요.</p>")
    return "".join(out)


def card_text_checks(top: dict, verdicts: dict) -> tuple[str, ...]:
    """썸네일 체크 3줄과 같은 조건 문구."""
    if "deadline" in top:
        kind = top["base_kind"] if top.get("base_kind") in CHECKS else top["kind"].split("(")[0]
        return CHECKS.get(kind, LEASE_CHECKS)
    route = best_route(verdicts.get(applyhome_key(top))).replace("❔", "")
    return CHECKS.get(route, LOOSE_CHECKS)


def build_post(blog: dict, sale_show, verdicts, degraded, lease_show, rest, intro, today, images) -> str:
    """블로그 본문. 그래프는 images 에 {cid: png} 로 채운다."""
    facts = "".join(f"<li>{escape(f)}</li>" for f in blog.get("facts") or [])
    # intro = 읽기용 본문(readable_html). 그 아래 '📂 자세히 보기'에 지금까지 보내던 정보를 그대로 둔다
    out = [
        intro,
        HR + f"<p style='{HEAD}'>📂 자세히 보기</p>"
        f"<p style='{SMALL}'>공고별 상세 정보(위치·일정·주택·시세 그래프·특공 판정·신청 방법)를 모았어요.</p>",
        "<div style='background:#f6f8fa;border-radius:8px;padding:14px 18px;margin-bottom:20px'>"
        f"<p style='margin:0 0 6px'><b>👤 이 글의 모델 — {escape(blog['category'])}</b></p>"
        f"<p style='margin:0 0 6px'>{escape(blog.get('intro') or '')}</p>"
        f"<ul style='margin:0;padding-left:18px;font-size:14px'>{facts}</ul></div>",
    ]
    if sale_show:
        html = notify_email.build_html(sale_show, verdicts, today, degraded, applyhome_key, images)
        html = html.replace(f"🏠 오늘의 청약 · {today:%Y년 %m월 %d일}", "🏠 분양 (청약홈)")
        out.append(body_of(html).replace(notify_email.CHECKS_MARK, ""))
    if lease_show:
        out.append(lease.html_section(lease_show, today))
    if rest:
        out.append("<h3 style='margin:24px 0 6px'>📋 계속 접수 중인 공고</h3><ul style='font-size:14px'>"
                   + "".join(one_liner(x, today) for x in rest) + "</ul>")
    out.append(
        "<p style='color:#888;font-size:12px;margin-top:24px'>"
        "※ 이 글의 인물은 실제 사람이 아닌 가상 모델입니다.<br>"
        "※ 청약홈·LH 공공데이터를 바탕으로 프로그램이 자동 판정한 결과이며, 참고용입니다. "
        "신청 전 반드시 모집공고문으로 확인하세요.</p>"
        f"<p style='{SMALL}'>[참고 출처 및 기준일] 한국부동산원 청약홈 · LH청약플러스 · 국토교통부 실거래가 공개시스템 · "
        f"확인일 {today.year}년 {today.month}월 {today.day}일</p>"
    )
    return "".join(out)


TAG_MAX = 20  # 네이버 태그 상한은 30개. 너무 많으면 광고성으로 보여 20개로 묶는다


def plain_name(name) -> str:
    """괄호 속(공공분양·본청약 등)을 뺀 단지명."""
    return re.sub(r"\s*\([^)]*\)", "", str(name)).strip()


def compact(name: str) -> str:
    """태그용 단지명: 괄호 속과 공백·기호를 뺀다. '인천계양 A17블록 신혼희망타운(공공분양)' → '인천계양A17블록신혼희망타운'."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", re.sub(r"\([^)]*\)", "", str(name)))


def town(address: str) -> str:
    """주소의 시·군·구 이름(접미사 뺌). '인천광역시 부평구 산곡동' → '부평'."""
    parts = str(address).split()
    if parts and parts[0].endswith("특별자치시"):  # 세종은 시·군·구가 없다
        return parts[0].removesuffix("특별자치시")
    return re.sub(r"(시|군|구)$", "", parts[1]) if len(parts) > 1 else ""


def best_route(verdict) -> str:
    """제목에 쓸 대표 신청 방법: 특공이 있으면 특공, 없으면 첫 경로."""
    routes = (verdict.possible + verdict.conditional) if verdict else []
    names = [notify_email.short_route(r.name) for r in routes]
    return next((n for n in names if "특공" in n), names[0] if names else "")


def post_title(blog: dict, top: dict, route: str, count: int, today: date) -> str:
    """네이버는 제목 앞쪽 단어를 검색에 크게 본다. 유입 검색어가 '단지명 + 평수·오피스텔·민간임대'
    꼴이라(2026-10-13 통계) 단지명 다음에 유형과 평수·분양가를 둔다. count 는 본문 도입부에 있다."""
    when = f"({blog['label']} 기준, {today.month}/{today.day})"
    if "deadline" in top:  # LH 임대
        return f"{lease_title(top['name'])[:28]} {top['kind'].split('(')[0]} 보증금·입주 자격 {when}"
    name = plain_name(top["name"])[:28]
    kind = detail.kind_word(top).split("(")[0].replace(" 물량", "")
    kind = "" if kind.split()[-1] in name else kind  # '… 오피스텔 오피스텔'처럼 겹치지 않게
    what = f"{route} 자격" if "특공" in route else "청약 자격"
    return " ".join(x for x in (name, kind, "평수·분양가·" + what, when) if x)


def post_tags(blog: dict, sale_show: list, lease_show: list, verdicts: dict, today: date) -> list[str]:
    """인물 고정 태그 + 이번 글의 단지명·동네·전형·달. 검색은 '단지명', '부평 청약'처럼 들어온다."""
    tags = list(blog.get("tags") or [])[:6]
    for x in sale_show:
        tags.append(compact(x["name"]))
        block = re.search(r"([A-Za-z]{1,2}\d+(?:-\d+)?)\s*(?:블록|BL)", str(x["name"]))
        if block:  # '의왕청계2 a1' 처럼 블록명으로 찾는 검색어
            tags.append(compact(str(x["name"]).split()[0]) + block.group(1).upper())
        if town(x.get("address")):
            tags.append(f"{town(x['address'])}청약")
        route = best_route(verdicts.get(applyhome_key(x)))
        if route:
            tags.append(route.replace(" ", "").replace("❔", ""))
    tags += [x["kind"].split("(")[0].replace("·", "") for x in lease_show]
    tags += [f"{today.month}월청약", "청약자격", "청약공고"]
    return [t for t in dict.fromkeys(tags) if t][:TAG_MAX]


def drop_suspect_numbers(items: list[dict]) -> list[validate.Issue]:
    """공개 글에 틀린 시세가 나가지 않게, 의심스러운 비교는 글에서 아예 뺀다(베타는 메모만 남긴다).

    산곡역 단지가 제천 시세와 비교돼 '193% 비쌈'으로 나간 일이 있었다(2026-10-12).
    - 위치를 못 찾음 → 메모(지도·역·시세가 빠진 채 나감)
    - 시세를 잰 시·군·구 코드가 공급지역 시·도가 아님 → 시세 삭제
    - 시세·주변 분양가와 차이가 GAP_LIMIT(%) 초과 → 그 비교 삭제
    """
    issues = []
    for item in items:
        where = str(item.get("name") or "")[:20]
        if not item.get("coords"):
            issues.append(validate.Issue(1, where, "위치를 찾지 못해 지하철역·시세 없이 나감"))
        m = item.get("market") or {}
        expected = market.SIDO_CODES.get((item.get("area_name") or "").strip())
        if m and expected and not str(m.get("lawd", "")).startswith(expected):
            item["market"] = None
            issues.append(validate.Issue(1, where, f"시세를 다른 시·도({m.get('lawd')})에서 잡아 시세를 빼고 보냄"))
        elif m and abs(m.get("gap_pct") or 0) > validate.GAP_LIMIT:
            item["market"] = None
            issues.append(validate.Issue(1, where, f"시세 대비 {m['gap_pct']:+.0f}%로 비정상이라 시세를 빼고 보냄"))
        p = item.get("presales") or {}
        if p and abs(p.get("gap_pct") or 0) > validate.GAP_LIMIT:
            item["presales"] = None
            issues.append(validate.Issue(1, where, f"주변 분양가 대비 {p['gap_pct']:+.0f}%로 비정상이라 빼고 보냄"))
    return issues


# docs/naver-seo.md 8장 '발행 전 1분 점검표'. 문서를 고치면 여기도 같이 고친다
CHECKLIST = (
    "<div style='border:1px solid #d6e4ff;background:#f5f8ff;border-radius:6px;padding:8px 12px;"
    "font-size:12px;line-height:1.8;margin-bottom:12px'><b>발행 전 점검 (docs/naver-seo.md)</b><br>"
    + "<br>".join("☐ " + item for item in (
        "제목: 위 제목 복사, 과장 표현 없음",
        "썸네일: 카드가 첫 이미지, 대표 이미지로 지정",
        "검증 메모: '없음'이거나, 빨간 칸이면 공고문 확인 끝냄",
        "맞춤법: 붙여넣은 뒤 에디터의 맞춤법 검사 한 번 (직접 덧붙인 문장 포함)",
        "이미지: 발행 후 휴대폰 네이버 앱에서 썸네일·그래프가 보이는지 확인",
        "본문: 표·이미지·링크 깨짐 없음",
        "안내 문구: '가상 인물 · 판정은 참고용'이 맨 끝에 남아 있음",
        "태그: 붙여넣음, 관계없는 키워드 없음",
        "카테고리: 위 카테고리 선택",
        "예약: 위 예약 시각으로 예약 (즉시 발행 아님)",
    )) + "</div>"
)


# 원고 이미지를 올려 두는 공개 주소. 저장소가 공개여야 로그인 없이 열린다
IMAGE_BASE_URL = "https://raw.githubusercontent.com/JUSEONGHONG-CMD/cheongyak-blog/main/public"
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"


def place_images(post: str, images: dict[str, bytes], folder: Path, base_url: str
                 ) -> tuple[str, list[tuple[str, bytes, str]]]:
    """본문 이미지를 공개 주소로 바꾸고, 같은 이미지를 번호 붙인 첨부 목록으로도 돌려준다.

    메일 첨부(cid) 이미지는 Gmail 에서 복사하면 로그인 전용 주소로 붙어, 네이버가 사진으로 가져오지 못하고
    모바일·방문자에게 '존재하지 않는 이미지'가 된다. 로그인 없이 열리는 공개 주소의 이미지는 Gmail 에서
    통째로 복사해도 사진으로 들어가고 모바일에서도 보였다(2026-10-13 시험, 본문·테두리 상자 안 모두).
    그래서 이미지를 저장소 public/<날짜>/<별칭>/ 에 쓰고(Actions 가 커밋), 본문은 그 주소를 가리킨다.
    첨부는 사진이 빠졌을 때 직접 넣을 예비용이다.
    """
    folder.mkdir(parents=True, exist_ok=True)
    order = list(dict.fromkeys(re.findall(r"cid:([^'\"]+)", post)))
    files = []
    for number, cid in enumerate(order, 1):
        if cid.startswith("thumb"):
            name, slug = "썸네일", "thumb"
        else:  # 그래프 바로 앞의 공고 카드 제목(h3)이 그 그래프의 단지
            head = re.findall(r"<h3[^>]*>(.*?)</h3>", post[: post.find(f"cid:{cid}")], re.S)
            # 카드 제목 '🔴 마감임박 1. 쌍용 더 플래티넘 한강' 에서 순번 앞부분을 떼고 단지명만
            title = re.sub(r"^.*?\d+\.\s*", "", re.sub(r"<[^>]+>", "", head[-1]).strip()) if head else ""
            name, slug = "시세그래프_" + compact(title)[:20], "chart"
        public_name = f"{number:02d}_{slug}.png"  # 주소에는 한글을 쓰지 않는다
        (folder / public_name).write_bytes(images[cid])
        files.append((f"{number:02d}_{name}.png", images[cid], "image/png"))
        post = post.replace(f"cid:{cid}", f"{base_url}/{public_name}")
    return post, files


def build_mail(blog: dict, title: str, tags: list[str], issues: list, post: str) -> str:
    """운영자용 메일: 위에는 발행 정보, 구분선 아래는 그대로 복사할 본문."""
    notes = "<br>".join(escape(str(i)) for i in issues) or "없음"
    row = "<tr><td style='padding:4px 10px;color:#666'>{}</td><td style='padding:4px 10px'>{}</td></tr>"
    return (
        "<html><body style='font-family:-apple-system,\"Malgun Gothic\",sans-serif;"
        "max-width:900px;margin:0 auto;padding:16px;color:#222'>"
        "<table style='background:#fffbe6;border:1px solid #f0d78c;border-collapse:collapse;"
        "font-size:13px;margin-bottom:12px'>"
        + row.format("카테고리", escape(blog["category"]))
        + row.format("제목", f"<b>{escape(title)}</b>")
        + row.format("태그 (입력란에 붙여넣기)", f"<b>{escape(', '.join(tags))}</b>")
        + row.format("예약 발행", escape(blog.get("publish_time") or "-"))
        + row.format("사진", "본문을 그대로 복사하면 썸네일·그래프가 사진으로 함께 들어갑니다(공개 주소 이미지). "
                     "빠진 사진만 첨부 <b>01_썸네일 · 02_시세그래프…</b>로 넣으세요.")
        + (row.format("검증 메모", notes) if not issues else
           "<tr><td style='padding:4px 10px;color:#c33'><b>검증 메모</b></td>"
           f"<td style='padding:4px 10px;background:#fde8e8;color:#c33'><b>{notes}</b></td></tr>")
        + "</table>" + CHECKLIST
        + "<p style='color:#c33;font-size:13px'>▼ 아래 선부터 끝까지 복사해 스마트에디터에 붙여 넣으세요 ▼</p>"
        "<hr>" + post + "</body></html>"
    )


def last_posted(alias: str) -> str:
    """이 인물 원고가 마지막으로 나간 날(YYYY-MM-DD). 한 번도 없으면 ''."""
    return max((r.get("last_sent") or r.get("first_sent") or "" for r in SeenStore(alias).data.values()), default="")


def gather(profile, keys, cache, today) -> dict:
    """한 인물이 넣을 수 있는 공고 전부(분양·임대). 배정 전 단계."""
    sale, verdicts, degraded = collect_sale(profile, keys, cache, today)
    leases = collect_lease(profile, keys, cache, today)
    # 글은 그날 08:30 이후 올라간다. 오늘 마감 공고는 읽는 사람이 넣을 시간이 없어 뺀다
    return {"sale": [x for x in sale if x.get("days_left") != 0], "verdicts": verdicts, "degraded": degraded,
            "leases": [x for x in leases if x["deadline"] > today]}


# 대상층이 정해진 LH 임대 유형. 든든전세·국민·영구·공공임대처럼 누구나 넣는 유형보다 그 인물에게 더 맞다
TARGETED = ("청년", "신혼", "신생아", "고령", "다자녀", "대학생", "한부모")
SPECIAL = ("특공", "특별공급")


def fit(item: dict, pool: dict, profile: dict) -> tuple[int, int, int]:
    """이 공고가 이 인물에게 얼마나 맞나: (전형 맞춤 3·2·1, 확실함 1·0, 사는 시·도 1·0). 클수록 잘 맞는다.

    분양: 특별공급 3 > 일반공급 2 > 누구나 넣는 공고(임의공급·무순위·오피스텔 등) 1.
    임대: 대상층이 정해진 유형(청년·신혼·고령자 …) 3 > 누구나 넣는 유형 1.
    """
    home = str((profile.get("applicant") or {}).get("region") or "")
    if "deadline" in item:  # LH 임대
        text = f"{item.get('kind', '')} {item.get('track', '')}"
        level = 3 if any(w in text for w in TARGETED) else 1
        return level, int(item.get("sure", True)), int(bool(home) and home in str(item.get("region") or ""))
    verdict = pool["verdicts"].get(applyhome_key(item))
    possible = verdict.possible if verdict else []
    routes = (verdict.possible + verdict.conditional) if verdict else []
    special = [r for r in routes if any(w in r.name for w in SPECIAL)]
    level = 1 if item.get("category") in ANYONE else 3 if special else 2
    sure = any(r in possible for r in special) if special else bool(possible)
    return level, int(sure), int(home == str(item.get("area_name") or ""))


def assign(pools: dict[str, dict], profiles: dict[str, dict], posted: dict) -> dict[str, str]:
    """아직 어느 인물로도 안 나간 공고마다 가장 알맞은 인물 하나를 정한다. {공고 키: 별칭}

    같은 공고가 여러 인물 원고에 겹쳐 나가지 않게 한다(2026-10-26 요청). 맞춤 점수(fit)가 높은 인물,
    같으면 후보 공고가 적은 인물(쓸 소재가 부족한 쪽), 그다음 글이 오래 안 나간 인물에게 준다.
    """
    candidates: dict[str, list[tuple]] = {}
    sizes = {a: len(p["sale"]) + len(p["leases"]) for a, p in pools.items()}
    for alias, pool in pools.items():
        for item in (*pool["sale"], *pool["leases"]):
            key = SeenStore.key(item)
            if key not in posted:
                rank = fit(item, pool, profiles[alias])
                candidates.setdefault(key, []).append(
                    (tuple(-r for r in rank), sizes[alias], last_posted(alias), alias))
    return {key: min(options)[-1] for key, options in candidates.items()}


def run_one(alias, profile, args, pool, owner, posted, today) -> str | None:
    """한 인물. 'sent'(미리보기는 만들었음) · 'skip'(새 공고 없음) · None(실패).

    pool 은 gather 결과, owner 는 assign 결과(공고 키 → 그 공고를 쓸 인물), posted 는 이미 나간 공고 키 → {인물: 처음 쓴 날}.
    """
    blog = profile["blog"]
    seen = SeenStore(alias)
    sale, leases = pool["sale"], pool["leases"]
    verdicts, degraded = pool["verdicts"], pool["degraded"]
    mine = lambda x: owner.get(SeenStore.key(x)) == alias  # noqa: E731
    sale_new = sorted((x for x in sale if mine(x)), key=lambda x: x.get("category") in ANYONE)
    lease_new = [x for x in leases if mine(x)]
    others = len([x for x in (*sale, *leases) if SeenStore.key(x) in owner and not mine(x)])
    print(f"      분양 {len(sale)}건(새 {len(sale_new)}) · 임대 {len(leases)}건(새 {len(lease_new)})"
          + (f" · 다른 인물에게 간 공고 {others}건" if others else ""))
    if not sale_new and not lease_new:
        print("      새 공고 없음 — 건너뜀")
        return "skip"

    sale_show, lease_show = split_detail(sale_new, lease_new)
    for number, x in enumerate(sale_show, 1):  # 순서를 바꿨으니 표의 # 도 다시 매긴다
        x["rank"] = number
    shown = {id(x) for x in (*sale_show, *lease_show)}
    def ours(x):  # 이 인물 몫이거나, 이 인물이 가장 먼저 쓴 공고만 (배정 전에 여러 인물이 함께 쓴 공고 포함)
        key = SeenStore.key(x)
        if key in owner:
            return owner[key] == alias
        first = min(posted.get(key, {alias: ""}).items(), key=lambda kv: (kv[1], kv[0]))[0]
        return first == alias
    rest = [x for x in (*sale, *leases) if id(x) not in shown and ours(x)]
    issues = [*drop_suspect_numbers(sale_show),
              *validate.check_data(sale_show, verdicts, applyhome_key, today, profile.get("filters") or {}),
              *validate.check_leases(lease_show, today)]
    images: dict[str, bytes] = {}
    count = len(sale_new) + len(lease_new)
    intro = readable_html(profile, sale_show, lease_show, verdicts, count,
                          intro_html(alias, blog, sale_show, lease_show, verdicts, count, today, short=True), today)
    post = build_post(blog, sale_show, verdicts, degraded, lease_show, rest, intro, today, images)
    card = thumbnail.card_png(**card_text(alias, profile, sale_show, lease_show, verdicts, today))
    if card:  # 네이버는 첫 이미지를 검색 썸네일로 쓴다
        images["thumb@blog"] = card
        post = "<p><img src='cid:thumb@blog' width='540' alt='대표 이미지'></p>" + post
    issues += validate.check_output(sale_show, post, images)

    top = (sale_show or lease_show)[0]
    route = best_route(verdicts.get(applyhome_key(top))) if sale_show else top["kind"].split("(")[0]
    title = post_title(blog, top, route, count, today)
    tags = post_tags(blog, sale_show, lease_show, verdicts, today)
    # 본문 끝 해시태그: 본문을 복사하면 함께 따라간다
    post += f"<p style='color:#06c;font-size:13px'>{escape(' '.join(f'#{t}' for t in tags))}</p>"
    if args.preview:  # 미리보기는 이미지를 미리보기 폴더에 두고 상대 경로로
        folder = Path(args.preview) / alias
        post, files = place_images(post, images, folder, alias)
    else:
        sub = f"{today:%Y-%m-%d}/{alias}"
        post, files = place_images(post, images, PUBLIC_DIR / sub,
                                   f"{env('IMAGE_BASE_URL', required=False) or IMAGE_BASE_URL}/{sub}")
    mail = build_mail(blog, title, tags, issues, post)

    if args.preview:
        path = Path(args.preview) / f"{alias}.html"
        path.write_text(mail, encoding="utf-8")
        print(f"      미리보기: {path.resolve()}")
        return "sent"
    to = {"notify": {"email": {"to": [env("BLOG_TO", required=False) or env("GMAIL_ADDRESS")]}}}
    tag = "[시험] " if args.test else ""
    if not notify_email.send(to, mail, today, {}, subject=f"{tag}[블로그 원고] {title}", attachments=files):
        return None
    if args.test:  # 시험 발송은 기록을 남기지 않아 같은 공고가 실제 원고에 그대로 나간다
        return "sent"
    for x in (*sale_show, *lease_show):
        seen.mark(x, today)
    seen.prune(today)
    seen.save()
    return "sent"


def run(args: argparse.Namespace) -> int:
    today = date.today()
    everyone = load_profiles()
    profiles = everyone
    if args.only:
        wanted = {a.strip() for a in args.only.split(",") if a.strip()}
        profiles = {k: v for k, v in everyone.items() if k in wanted}
    # 오래 안 나간 인물부터. 못 나간 인물의 새 공고는 기록이 안 남아 다음 발행일로 넘어간다
    order = sorted(profiles, key=lambda a: (last_posted(a), a))
    print(f"하루 최대 {args.max}편 · 순서: {', '.join(order)}")
    keys = {
        "service": env("ODCLOUD_SERVICE_KEY"),
        "kakao": env("KAKAO_REST_API_KEY", required=False) or "",
    }
    cache = geo.GeoCache()
    failed = []
    # 배정은 언제나 전원 기준으로 한다(--only 로 일부만 보낼 때도 다른 인물 몫을 가져오지 않게)
    pools = {}
    for alias, profile in everyone.items():
        try:
            pools[alias] = gather(profile, keys, cache, today)
        except Exception:  # 한 인물의 오류가 다른 인물 원고를 막지 않게 한다
            traceback.print_exc()
            failed.append(alias)
        finally:
            cache.save()
    posted: dict[str, dict[str, str]] = {}  # 공고 키 → {쓴 인물: 처음 쓴 날}
    for alias in everyone:
        for key, record in SeenStore(alias).data.items():
            posted.setdefault(key, {})[alias] = record.get("first_sent") or ""
    owner = assign(pools, everyone, posted)
    shared = sum(1 for k in owner if sum(any(SeenStore.key(x) == k for x in (*p["sale"], *p["leases"]))
                                          for p in pools.values()) > 1)
    print(f"새 공고 {len(owner)}건 배정 · 여러 인물에 해당하던 공고 {shared}건은 한 명에게만")
    sent = 0
    for alias in order:
        if alias not in pools:
            continue
        if sent >= args.max:
            break
        profile = profiles[alias]
        profile["blog"]["publish_time"] = SLOTS[sent % len(SLOTS)]
        print(f"\n===== {alias} · {profile['blog']['category']} =====")
        try:
            ok = run_one(alias, profile, args, pools[alias], owner, posted, today)
        except Exception:  # 한 인물의 오류가 다른 인물 원고를 막지 않게 한다
            traceback.print_exc()
            ok = False
        finally:
            cache.save()
        if ok is None:
            failed.append(alias)
        elif ok == "sent":
            sent += 1
    print(f"\n원고 {sent}편" + (f" · 실패: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="청약 블로그 원고 생성기")
    parser.add_argument("--preview", metavar="DIR", help="메일을 보내지 않고 DIR/<별칭>.html 로 저장 (발송 이력도 안 남김)")
    parser.add_argument("--test", action="store_true", help="메일은 보내되 제목에 [시험]을 붙이고 발송 이력은 안 남김")
    parser.add_argument("--only", metavar="ALIAS", help="이 별칭들만 (쉼표로 여러 명)")
    parser.add_argument("--max", type=int, default=int(os.environ.get("BLOG_DAILY_MAX") or 3),
                        help="한 번에 보낼 원고 수 상한 (기본 3, 환경변수 BLOG_DAILY_MAX)")
    args = parser.parse_args()
    try:
        return run(args)
    except MissingKey as exc:
        print(f"[설정 오류] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if os.environ.get("SELFTEST"):
        geo.selfcheck()
        notice_pdf.selfcheck()
        # 배정: 같은 공고는 특공 대상 인물에게, 이미 나간 공고는 아무에게도
        from .eligibility import Route, Verdict
        apt = {"category": "APT", "house_manage_no": "1", "pblanc_no": "1", "area_name": "경기"}
        loose = {"category": "임의공급", "house_manage_no": "2", "pblanc_no": "2", "area_name": "경기"}
        old = {"category": "APT", "house_manage_no": "3", "pblanc_no": "3", "area_name": "경기"}
        couple = {"verdicts": {applyhome_key(apt): Verdict(routes=[Route("신혼부부 특별공급 (우선)", "가능", "")]),
                               applyhome_key(old): Verdict(routes=[Route("일반공급 추첨제", "가능", "")])},
                  "sale": [apt, old], "leases": []}
        single = {"verdicts": {applyhome_key(apt): Verdict(routes=[Route("일반공급 추첨제", "가능", "")]),
                               applyhome_key(loose): Verdict(routes=[Route("임의공급", "가능", "")])},
                  "sale": [apt, loose], "leases": []}
        people = {"zz-couple": {"applicant": {"region": "인천"}}, "zz-single": {"applicant": {"region": "경기"}}}
        got = assign({"zz-couple": couple, "zz-single": single}, people, {applyhome_key(old): {"zz-other": "2026-10-01"}})
        assert got == {applyhome_key(apt): "zz-couple", applyhome_key(loose): "zz-single"}, got
        youth = {"kind": "청년(본인)", "deadline": date(2026, 11, 1), "region": "서울특별시", "seen_key": "lease:a"}
        dd = {"kind": "든든전세", "deadline": date(2026, 11, 1), "region": "서울특별시", "seen_key": "lease:b"}
        assert fit(youth, {}, {"applicant": {"region": "서울"}}) == (3, 1, 1)
        assert fit(dd, {}, {"applicant": {"region": "경기"}}) == (1, 1, 0)
        assert split_detail([1] * 5, [2] * 5) == ([1] * 3, [2] * 3)
        assert split_detail([1] * 5, [2]) == ([1] * 5, [2])
        assert split_detail([1], [2] * 9) == ([1], [2] * 5)
        assert split_detail([], [2] * 9) == ([], [2] * 6)
        wrong = {"name": "산곡", "area_name": "인천", "coords": (37.1, 128.2),
                 "market": {"lawd": "43150", "gap_pct": 193.0}, "presales": {"gap_pct": 26.0}}
        assert len(drop_suspect_numbers([wrong])) == 1 and wrong["market"] is None and wrong["presales"]
        odd = {"name": "x", "area_name": "인천", "coords": (37.5, 126.7), "market": {"lawd": "28237", "gap_pct": 120.0}}
        assert len(drop_suspect_numbers([odd])) == 1 and odd["market"] is None
        fine = {"name": "y", "area_name": "인천", "coords": (37.5, 126.7), "market": {"lawd": "28237", "gap_pct": 6.0}}
        assert drop_suspect_numbers([fine]) == [] and fine["market"]
        assert "위치를 찾지 못해" in str(drop_suspect_numbers([{"name": "z"}])[0])
        assert lease_title("[서울지역본부] 26년 2차 비분양전환형 든든전세주택 입주자 모집공고") == "26년 2차 비분양전환형 든든전세주택"
        assert lease_title("양주회천 A25BL 영구임대주택 추가입주자 모집 공고('26.09.21)") == "양주회천 A25BL 영구임대주택"
        import io
        buf = io.StringIO()
        Redacted(buf).write("GET /api?serviceKey=abc%2Bdef&page=1 실패")
        assert buf.getvalue() == "GET /api?serviceKey=***&page=1 실패", buf.getvalue()
        from .detail import summary_sentence
        s = summary_sentence({"name": "옥포 힐스테이트", "address": "경상남도 거제시 옥포동 1", "category": "APT",
                              "house_type": "민영", "matched_models": [{"area": 59.9}, {"area": 84.9}],
                              "price_min": 310_000_000, "price_max": 420_000_000, "general_end": date(2026, 10, 20)})
        assert s == "옥포 힐스테이트는 경상남도 거제시의 민간분양 아파트로, 전용 60·85㎡(18·26평), 분양가 3.1~4.2억이며 10월 20일에 접수가 끝나요.", s
        t = post_title({"label": "사회초년생"}, {"name": "당산역 더클래스 한강", "category": "오피스텔등",
                        "house_type": "오피스텔"}, "오피스텔 청약", 1, date(2026, 10, 12))
        assert t == "당산역 더클래스 한강 오피스텔 평수·분양가·청약 자격 (사회초년생 기준, 10/12)", t
        assert compact("인천계양 A17블록 신혼희망타운(공공분양)(본청약)") == "인천계양A17블록신혼희망타운"
        assert town("인천광역시 부평구 산곡동 10번지") == "부평" and town("경기도 수원시 영통구") == "수원"
        assert town("세종특별자치시 산울동") == "세종"
        print("ok")
        raise SystemExit(0)
    raise SystemExit(main())
