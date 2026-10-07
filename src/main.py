"""청약 블로그 원고 생성기 진입점.

가상 인물(profiles/*.yaml)마다 지원 가능한 분양·LH 임대 공고를 골라 네이버 블로그에
붙여 넣을 원고를 만들고, 운영자에게 메일로 보낸다(인물 한 명 = 글 한 편 = 메일 한 통).
지난 원고 뒤로 새로 뜬 공고가 없는 인물은 그날 건너뛴다.

판정·수집 코드는 청약 알리미 베타에서 복사해 왔다. 베타와 따로 운용한다.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date
from html import escape
from pathlib import Path

from . import (
    applyhome,
    builder,
    eligibility,
    geo,
    lease,
    market,
    notice_pdf,
    notify_email,
    presale,
    ranking,
    regulation,
    validate,
)
from .config import MissingKey, env, load_profiles
from .state import SeenStore

DETAIL_MAX = 6  # 글 하나에 자세히 쓰는 새 공고 수. 나머지는 한 줄 목록


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


def build_post(blog: dict, sale_show, verdicts, degraded, lease_show, rest, count, today, images) -> str:
    """블로그 본문. 그래프는 images 에 {cid: png} 로 채운다."""
    facts = "".join(f"<li>{escape(f)}</li>" for f in blog.get("facts") or [])
    out = [
        "<div style='background:#f6f8fa;border-radius:8px;padding:14px 18px;margin-bottom:20px'>"
        f"<p style='margin:0 0 6px'><b>👤 이 글의 모델 — {escape(blog['category'])}</b></p>"
        f"<p style='margin:0 0 6px'>{escape(blog.get('intro') or '')}</p>"
        f"<ul style='margin:0;padding-left:18px;font-size:14px'>{facts}</ul></div>",
        f"<p>{today.month}월 {today.day}일 기준, 이 조건으로 <b>새로 지원할 수 있는 공고가 {count}건</b> 나왔습니다."
        + (f" 그중 {len(sale_show) + len(lease_show)}건을 자세히, 나머지는 아래 목록으로 정리했습니다."
           if count > len(sale_show) + len(lease_show) else "")
        + " 비슷한 상황이라면 참고해 보세요.</p>",
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
        + row.format("태그", escape(" ".join(f"#{t}" for t in tags)))
        + row.format("예약 발행", escape(blog.get("publish_time") or "-"))
        + row.format("검증 메모", notes)
        + "</table><p style='color:#c33;font-size:13px'>▼ 아래 선부터 끝까지 복사해 스마트에디터에 붙여 넣으세요 ▼</p>"
        "<hr>" + post + "</body></html>"
    )


def run_one(alias, profile, args, keys, cache, today) -> bool:
    """한 인물. 원고를 보냈으면(미리보기는 만들었으면, 새 공고가 없어 건너뛰었으면) True."""
    blog = profile["blog"]
    seen = SeenStore(alias)
    sale, verdicts, degraded = collect_sale(profile, keys, cache, today)
    leases = collect_lease(profile, keys, cache, today)
    sale_new = [x for x in sale if seen.is_new(x)]
    lease_new = [x for x in leases if seen.is_new(x)]
    print(f"      분양 {len(sale)}건(새 {len(sale_new)}) · 임대 {len(leases)}건(새 {len(lease_new)})")
    if not sale_new and not lease_new:
        print("      새 공고 없음 — 건너뜀")
        return True

    sale_show, lease_show = split_detail(sale_new, lease_new)
    shown = {id(x) for x in (*sale_show, *lease_show)}
    rest = [x for x in (*sale, *leases) if id(x) not in shown]
    issues = [*validate.check_data(sale_show, verdicts, applyhome_key, today, profile.get("filters") or {}),
              *validate.check_leases(lease_show, today)]
    images: dict[str, bytes] = {}
    count = len(sale_new) + len(lease_new)
    post = build_post(blog, sale_show, verdicts, degraded, lease_show, rest, count, today, images)
    issues += validate.check_output(sale_show, post, images)

    top = (sale_show or lease_show)[0]["name"]
    title = f"[{blog['label']}] {today:%m/%d} 새 청약 공고 {count}건 — {str(top)[:30]}"
    tags = list(dict.fromkeys([*(blog.get("tags") or []), *(x["kind"].split("(")[0] for x in lease_show)]))
    mail = build_mail(blog, title, tags, issues, post)

    if args.preview:
        path = Path(args.preview) / f"{alias}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        for cid, png in images.items():
            (path.parent / f"{cid.split('@')[0]}.png").write_bytes(png)
            mail = mail.replace(f"cid:{cid}", f"{cid.split('@')[0]}.png")
        path.write_text(mail, encoding="utf-8")
        print(f"      미리보기: {path.resolve()}")
        return True
    to = {"notify": {"email": {"to": [env("BLOG_TO", required=False) or env("GMAIL_ADDRESS")]}}}
    if not notify_email.send(to, mail, today, images, subject=f"[블로그 원고] {title}"):
        return False
    for x in (*sale_show, *lease_show):
        seen.mark(x, today)
    seen.prune(today)
    seen.save()
    return True


def run(args: argparse.Namespace) -> int:
    today = date.today()
    profiles = load_profiles()
    if args.only:
        wanted = {a.strip() for a in args.only.split(",") if a.strip()}
        profiles = {k: v for k, v in profiles.items() if k in wanted}
    keys = {
        "service": env("ODCLOUD_SERVICE_KEY"),
        "kakao": env("KAKAO_REST_API_KEY", required=False) or "",
    }
    cache = geo.GeoCache()
    failed = []
    for alias, profile in profiles.items():
        print(f"\n===== {alias} · {profile['blog']['category']} =====")
        try:
            ok = run_one(alias, profile, args, keys, cache, today)
        except Exception:  # 한 인물의 오류가 다른 인물 원고를 막지 않게 한다
            traceback.print_exc()
            ok = False
        finally:
            cache.save()
        if not ok:
            failed.append(alias)
    print(f"\n완료 {len(profiles) - len(failed)}/{len(profiles)}명" + (f" · 실패: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="청약 블로그 원고 생성기")
    parser.add_argument("--preview", metavar="DIR", help="메일을 보내지 않고 DIR/<별칭>.html 로 저장 (발송 이력도 안 남김)")
    parser.add_argument("--only", metavar="ALIAS", help="이 별칭들만 (쉼표로 여러 명)")
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
        print("ok")
        raise SystemExit(0)
    raise SystemExit(main())
