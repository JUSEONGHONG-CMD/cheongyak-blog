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
    return (f"<li>[분양 {escape(str(item.get('category') or ''))}] "
            f"<a href='{escape((item.get('url') or '').strip())}'>{escape(str(item.get('name') or ''))}</a>"
            + (f" · D-{left}" if left is not None else "") + "</li>")


OPENINGS = (
    "{m}월 {d}일, {label} 조건으로 새로 넣을 수 있는 공고가 {n}건 나왔습니다.",
    "{label} 기준으로 이번에 새로 열린 공고를 확인해 보니 {n}건이 있었어요.",
    "{label} 상황이라면 눈여겨볼 새 공고가 {n}건입니다 ({m}월 {d}일 기준).",
)


def dday(item: dict) -> str:
    """분양은 days_left, LH 임대는 deadline 으로 'D-9'."""
    left = item.get("days_left") if "deadline" not in item else (item["deadline"] - date.today()).days
    return "" if left is None else ("오늘 마감" if left == 0 else f"D-{left} 마감")


def intro_html(alias: str, blog: dict, sale_show, lease_show, verdicts, count: int, today: date) -> str:
    """원고마다 달라지는 도입부 2~4문장. 같은 틀이 반복되면 네이버가 유사 문서로 볼 수 있어
    여는 문장을 인물·날짜로 돌리고, 나머지는 그날 공고 숫자로 채운다."""
    variant = (sum(map(ord, alias)) + today.toordinal()) % len(OPENINGS)
    lines = [OPENINGS[variant].format(m=today.month, d=today.day, label=blog["label"], n=count)]
    if sale_show:
        top = sale_show[0]
        route = best_route(verdicts.get(applyhome_key(top)))
        where = " · ".join(x for x in (str(top.get("area_name") or ""), dday(top)) if x)
        line = f"가장 먼저 볼 곳은 <b>{escape(plain_name(top['name']))}</b>({escape(where)})으로"
        line += f", {escape(route)} 자격으로 넣을 수 있습니다." if route else " 신청할 수 있습니다."
        m = top.get("market") or {}
        if m.get("gap_pct") is not None and top.get("price_min"):
            side = "저렴" if m["gap_pct"] < 0 else "비쌉"
            line += (f" 분양가는 {detail.price_span(top)}으로 주변 시세보다 약 {abs(m['gap_pct']):.0f}% "
                     f"{side}니다.")
        lines.append(line)
        special = sum(1 for x in sale_show if "특공" in best_route(verdicts.get(applyhome_key(x))))
        if special > 1:
            lines.append(f"특별공급으로 넣을 수 있는 분양 공고는 {special}곳입니다.")
    if lease_show:
        kinds = list(dict.fromkeys(x["kind"].split("(")[0] for x in lease_show))
        first = min(x["deadline"] for x in lease_show)
        lines.append(f"LH 임대는 {escape('·'.join(kinds[:3]))} 등 {len(lease_show)}건이 새로 접수 중이고, "
                     f"가장 빠른 마감은 {first.month}월 {first.day}일입니다.")
    if count > len(sale_show) + len(lease_show):
        lines.append("나머지 공고는 아래 목록에 한 줄씩 정리했어요.")
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


def card_text(blog: dict, sale_show, lease_show, verdicts, count: int, today: date) -> tuple[str, ...]:
    """썸네일 카드 문구 (상단 띠, 기준일, 헤드라인, 마감 임박 단지, 전형·마감, 가까운 역)."""
    soonest = min((*sale_show, *lease_show), key=lambda x: days_left(x, today))
    route = (best_route(verdicts.get(applyhome_key(soonest))) if "deadline" not in soonest
             else soonest["kind"].split("(")[0]).replace("❔", "")
    if "deadline" in soonest:
        # LH 임대는 여러 곳의 집을 한 공고로 뽑아 역이 하나로 정해지지 않는다. 지역·호수를 적는다
        focus = f"마감 임박 공고 : {lease_title(soonest['name'])}"
        houses = f" {soonest['houses']}호" if soonest.get("houses") else ""
        footer = f"{soonest.get('region') or 'LH'} 일대{houses} 모집 · 집 위치는 공고문 확인"
    else:
        focus = f"마감 임박 단지 : {plain_name(soonest['name'])}"
        station = soonest.get("subway") or {}
        footer = (f"가까운 역 : {station['name']} 도보 {station['walk_min']}분" if station.get("name")
                  else "가까운 지하철역 없음 (반경 2km)")
    return (f"{blog['label']} 신청 가능 청약", f"{today.month}월 {today.day}일 기준", f"새 청약 공고 {count}건",
            focus,
            " · ".join(x for x in (route, dday(soonest)) if x), footer)


def build_post(blog: dict, sale_show, verdicts, degraded, lease_show, rest, intro, today, images) -> str:
    """블로그 본문. 그래프는 images 에 {cid: png} 로 채운다."""
    facts = "".join(f"<li>{escape(f)}</li>" for f in blog.get("facts") or [])
    out = [
        "<div style='background:#f6f8fa;border-radius:8px;padding:14px 18px;margin-bottom:20px'>"
        f"<p style='margin:0 0 6px'><b>👤 이 글의 모델 — {escape(blog['category'])}</b></p>"
        f"<p style='margin:0 0 6px'>{escape(blog.get('intro') or '')}</p>"
        f"<ul style='margin:0;padding-left:18px;font-size:14px'>{facts}</ul></div>",
        intro,
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
    """네이버는 제목 앞쪽 단어를 검색에 크게 본다. 사람들이 찾는 단지명·전형을 앞에 둔다."""
    name = plain_name(top["name"])[:28]
    what = route if "특공" in route else "청약"  # '오피스텔 오피스텔 청약'처럼 단지명과 겹치지 않게
    return f"{name} {what} 자격 — {blog['label']} 기준 새 공고 {count}건 ({today.month}/{today.day})"


def post_tags(blog: dict, sale_show: list, lease_show: list, verdicts: dict, today: date) -> list[str]:
    """인물 고정 태그 + 이번 글의 단지명·동네·전형·달. 검색은 '단지명', '부평 청약'처럼 들어온다."""
    tags = list(blog.get("tags") or [])[:6]
    for x in sale_show:
        tags.append(compact(x["name"]))
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
        + (row.format("검증 메모", notes) if not issues else
           "<tr><td style='padding:4px 10px;color:#c33'><b>검증 메모</b></td>"
           f"<td style='padding:4px 10px;background:#fde8e8;color:#c33'><b>{notes}</b></td></tr>")
        + "</table><p style='color:#c33;font-size:13px'>▼ 아래 선부터 끝까지 복사해 스마트에디터에 붙여 넣으세요 ▼</p>"
        "<hr>" + post + "</body></html>"
    )


def last_posted(alias: str) -> str:
    """이 인물 원고가 마지막으로 나간 날(YYYY-MM-DD). 한 번도 없으면 ''."""
    return max((r.get("last_sent") or r.get("first_sent") or "" for r in SeenStore(alias).data.values()), default="")


def run_one(alias, profile, args, keys, cache, today) -> str | None:
    """한 인물. 'sent'(미리보기는 만들었음) · 'skip'(새 공고 없음) · None(실패)."""
    blog = profile["blog"]
    seen = SeenStore(alias)
    sale, verdicts, degraded = collect_sale(profile, keys, cache, today)
    leases = collect_lease(profile, keys, cache, today)
    # 글은 그날 08:30 이후 올라간다. 오늘 마감 공고는 읽는 사람이 넣을 시간이 없어 뺀다
    sale = [x for x in sale if x.get("days_left") != 0]
    leases = [x for x in leases if x["deadline"] > today]
    sale_new = sorted((x for x in sale if seen.is_new(x)), key=lambda x: x.get("category") in ANYONE)
    lease_new = [x for x in leases if seen.is_new(x)]
    print(f"      분양 {len(sale)}건(새 {len(sale_new)}) · 임대 {len(leases)}건(새 {len(lease_new)})")
    if not sale_new and not lease_new:
        print("      새 공고 없음 — 건너뜀")
        return "skip"

    sale_show, lease_show = split_detail(sale_new, lease_new)
    for number, x in enumerate(sale_show, 1):  # 순서를 바꿨으니 표의 # 도 다시 매긴다
        x["rank"] = number
    shown = {id(x) for x in (*sale_show, *lease_show)}
    rest = [x for x in (*sale, *leases) if id(x) not in shown]
    issues = [*drop_suspect_numbers(sale_show),
              *validate.check_data(sale_show, verdicts, applyhome_key, today, profile.get("filters") or {}),
              *validate.check_leases(lease_show, today)]
    images: dict[str, bytes] = {}
    count = len(sale_new) + len(lease_new)
    intro = intro_html(alias, blog, sale_show, lease_show, verdicts, count, today)
    post = build_post(blog, sale_show, verdicts, degraded, lease_show, rest, intro, today, images)
    card = thumbnail.card_png(*card_text(blog, sale_show, lease_show, verdicts, count, today))
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
    mail = build_mail(blog, title, tags, issues, post)

    if args.preview:
        path = Path(args.preview) / f"{alias}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        for cid, png in images.items():
            (path.parent / f"{cid.split('@')[0]}.png").write_bytes(png)
            mail = mail.replace(f"cid:{cid}", f"{cid.split('@')[0]}.png")
        path.write_text(mail, encoding="utf-8")
        print(f"      미리보기: {path.resolve()}")
        return "sent"
    to = {"notify": {"email": {"to": [env("BLOG_TO", required=False) or env("GMAIL_ADDRESS")]}}}
    tag = "[시험] " if args.test else ""
    if not notify_email.send(to, mail, today, images, subject=f"{tag}[블로그 원고] {title}"):
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
    profiles = load_profiles()
    if args.only:
        wanted = {a.strip() for a in args.only.split(",") if a.strip()}
        profiles = {k: v for k, v in profiles.items() if k in wanted}
    # 오래 안 나간 인물부터. 못 나간 인물의 새 공고는 기록이 안 남아 다음 발행일로 넘어간다
    order = sorted(profiles, key=lambda a: (last_posted(a), a))
    print(f"하루 최대 {args.max}편 · 순서: {', '.join(order)}")
    keys = {
        "service": env("ODCLOUD_SERVICE_KEY"),
        "kakao": env("KAKAO_REST_API_KEY", required=False) or "",
    }
    cache = geo.GeoCache()
    failed = []
    sent = 0
    for alias in order:
        if sent >= args.max:
            break
        profile = profiles[alias]
        profile["blog"]["publish_time"] = SLOTS[sent % len(SLOTS)]
        print(f"\n===== {alias} · {profile['blog']['category']} =====")
        try:
            ok = run_one(alias, profile, args, keys, cache, today)
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
        assert compact("인천계양 A17블록 신혼희망타운(공공분양)(본청약)") == "인천계양A17블록신혼희망타운"
        assert town("인천광역시 부평구 산곡동 10번지") == "부평" and town("경기도 수원시 영통구") == "수원"
        assert town("세종특별자치시 산울동") == "세종"
        print("ok")
        raise SystemExit(0)
    raise SystemExit(main())
