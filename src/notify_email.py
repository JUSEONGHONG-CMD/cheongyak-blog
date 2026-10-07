"""매일 아침 청약 보고서를 메일로 보낸다.

베타는 메일로만 보낸다. 메일은 HTML 을 그대로 그려 주므로 표로 한눈에
비교할 수 있다. 보내는 계정 하나로 프로필마다 notify.email.to 에게 보낸다.

Gmail SMTP 를 쓴다. 2단계 인증을 켠 계정에서 발급한 앱 비밀번호가 필요하다.
"""

from __future__ import annotations

import io
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from html import escape

from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

from . import detail, strategy

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
TIMEOUT = 30

# 메일 클라이언트마다 CSS 지원이 제각각이라 스타일은 태그에 직접 넣는다.
TD = "padding:6px 10px;border-bottom:1px solid #eee;font-size:13px"
TH = "padding:6px 10px;border-bottom:2px solid #ccc;font-size:12px;text-align:left;color:#555"

LINE_COLOR = "#8ba3bd"     # 시세 추이 선
LINE_RECENT = "#2f5f8f"    # 가장 최근 시세 점
CHEAP = "#0a7"             # 분양가가 시세보다 쌀 때
PRICEY = "#c33"            # 비쌀 때


def _dday(item: dict) -> str:
    left = item.get("days_left")
    if left is None:
        return "미정"
    if left < 0:
        return "마감"
    return "오늘 마감" if left == 0 else f"D-{left}"


def _market_cell(item: dict) -> str:
    """시세 대비 분양가. 싼 쪽을 초록, 비싼 쪽을 빨강으로 물들인다."""
    m = item.get("market")
    if not m or "gap_pct" not in m:
        return "-"
    pct = m["gap_pct"]
    if pct < 0:
        cell = f"<span style='color:{CHEAP}'>{abs(pct):.0f}% 쌈</span>"
    else:
        cell = f"<span style='color:{PRICEY}'>{pct:.0f}% 비쌈</span>"
    # 지금 싼지만이 아니라 주변 시세가 오르는 중인지도 함께 봐야 판단이 선다
    if "change_pct" in m:
        change = m["change_pct"]
        arrow = "↗" if change > 0.5 else ("↘" if change < -0.5 else "→")
        cell += (
            f"<br><span style='color:#888;font-size:11px'>"
            f"시세 1년 {change:+.0f}% {arrow}</span>"
        )
    return cell


def _month_label(ym: str) -> str:
    """'202509' → '25.9'. 13개월을 늘어놓으면 해가 바뀌므로 연도도 같이 적는다."""
    return f"{ym[2:4]}.{int(ym[4:])}"


def _chart_png(points: list[dict], values: list[int], price: int, floor: float, top: float) -> bytes:
    """시세 추이 선그래프 PNG.

    그림 안 글자는 숫자만 쓴다. GitHub Actions 서버에는 한글 글꼴이 없어
    한글을 넣으면 네모로 깨진다. 설명은 그림 밖 HTML 에 적는다.
    """
    fig = Figure(figsize=(6.4, 1.9), dpi=150)
    ax = fig.subplots()
    xs = list(range(len(values)))
    last = xs[-1]

    ax.plot(xs, values, color=LINE_COLOR, linewidth=2, marker="o", markersize=3.5, zorder=3)
    ax.plot([last], [values[-1]], marker="o", markersize=6.5, color=LINE_RECENT, zorder=4)

    right = last + 0.4
    if price and floor <= price <= top:
        color = CHEAP if price < values[-1] else PRICEY
        ax.hlines(price, -0.4, last + 0.3, color=color, linestyles="--", linewidth=1.5, zorder=2)
        ax.text(last + 0.45, price, f"{price / 1e8:.1f}", color=color,
                va="center", ha="left", fontsize=8, fontweight="bold")
        right = last + 1.5

    ax.set_xlim(-0.4, right)
    ax.set_ylim(floor, top + (top - floor) * 0.08)
    ticks = [i for i in xs if (last - i) % 3 == 0]
    ax.set_xticks(ticks, [_month_label(points[i]["month"]) for i in ticks])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1e8:.1f}"))
    ax.yaxis.set_major_locator(MaxNLocator(4))
    ax.grid(axis="y", color="#eeeeee", linewidth=0.8)
    ax.tick_params(colors="#999999", labelsize=8, length=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#cccccc")
    fig.tight_layout(pad=0.4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def market_chart(item: dict, images: dict[str, bytes]) -> str:
    """주변 시세 추이 선그래프.

    Gmail 은 본문의 SVG 와 data: 이미지를 지우지만, 메일에 첨부하고 cid: 로
    가리킨 이미지는 보여 준다. 그래서 PNG 로 그려 images 에 담고 본문에는
    그 cid 만 적는다. 첨부는 send() 가 한다.
    """
    m = item.get("market") or {}
    series = m.get("series") or []
    if len(series) < 3:
        return ""

    points = list(reversed(series))  # 오래된 달이 왼쪽에 오도록
    values = [p["median"] for p in points]
    price = m.get("price") or 0

    top, bottom = max(values), min(values)
    span = top - bottom or 1
    floor = bottom - span * 0.30

    # 분양가까지 축에 담을지 그때그때 정한다. 분양가상한제 단지처럼 시세와 크게
    # 벌어진 공고에서 함께 담으면 시세 선이 납작해져 정작 보려던 추이가
    # 사라진다. 시세가 세로 높이의 이만큼은 차지할 때만 함께 담는다.
    MIN_TREND_SHARE = 0.40
    if price:
        wide_top = max(top, price)
        wide_floor = min(floor, price - span * 0.15)
        if span / (wide_top - wide_floor) >= MIN_TREND_SHARE:
            top, floor = wide_top, wide_floor

    cid = f"chart{len(images) + 1}@cheongyak"
    images[cid] = _chart_png(points, values, price, floor, top)

    caption = (
        f"주변 전용 {m['area']:.0f}㎡ · {m.get('cohort', '준공연도 무관')} 실거래 중위값"
        f" · 최근 {len(points)}개월"
    )
    if price:
        color = CHEAP if price < values[-1] else PRICEY
        if floor <= price <= top:
            note = "점선은 이 단지 분양가"
        else:
            side = "아래" if price < floor else "위"
            note = f"분양가 {price / 1e8:.1f}억은 그래프 {side}쪽 밖"
        caption_html = (
            f"{escape(caption)} · <span style='color:{color}'>{escape(note)}</span>"
        )
    else:
        caption_html = escape(caption)

    return (
        "<div style='margin-top:10px;padding-top:10px;border-top:1px dashed #e5e5e5'>"
        f"<div style='font-size:12px;color:#555;margin-bottom:6px'>📈 {caption_html}</div>"
        f"<img src='cid:{cid}' width='480' alt='주변 시세 추이 그래프' "
        "style='display:block;width:100%;max-width:480px;height:auto;border:0'>"
        "<div style='font-size:10px;color:#aaa;margin-top:4px'>"
        "세로축 단위 억 원 · 차이가 보이도록 0부터 그리지 않았습니다</div>"
        "</div>"
    )


def _routes_cell(verdict) -> str:
    routes = verdict.possible or verdict.conditional
    if not routes:
        return "-"
    return "<br>".join(escape(r.name) for r in routes)


CHECKS_MARK = "<!--checks-->"

BUTTON = (
    "display:inline-block;padding:5px 12px;margin:0 6px 10px 0;border-radius:4px;"
    "font-size:12px;font-weight:600;text-decoration:none"
)


def link_bar(item: dict, today) -> str:
    """공고 보기·청약 신청 버튼. 메일 앱이 CSS 를 지워도 링크는 남도록 a 태그로 만든다."""
    links = []
    url = (item.get("url") or "").strip()
    if url:
        links.append(
            f"<a href='{escape(url)}' style='{BUTTON};border:1px solid #ccc;color:#333'>"
            "📄 공고 보기</a>"
        )
    apply_url, state = detail.apply_link(item, today)
    if apply_url and state != "마감":
        where = " (LH청약플러스)" if "lh.or.kr" in apply_url else ""
        note = f" · {state}" if state else ""
        links.append(
            f"<a href='{escape(apply_url)}' style='{BUTTON};background:#06c;color:#fff'>"
            f"✍️ 청약 신청하기{where}{note}</a>"
        )
    return f"<div>{''.join(links)}</div>" if links else ""


SECTION = re.compile(r"^([①-⑳])\s*(.+?)(?:\s{2,}(.*))?$")
LABEL_TD = (
    "padding:7px 10px;border-bottom:1px solid #eee;background:#f6f8fa;font-size:12px;"
    "font-weight:600;color:#444;white-space:nowrap;vertical-align:top;width:84px"
)
VALUE_TD = "padding:7px 10px;border-bottom:1px solid #eee;font-size:13px;line-height:1.6"


def detail_table(lines: list[str], extra: dict[str, str] | None = None) -> str:
    """상세 문구를 항목·내용 두 칸짜리 표로 바꾼다.

    내용은 카카오톡과 같은 detail.body 문구를 그대로 쓴다. 매체마다 문구를
    따로 만들면 한쪽만 고쳐져 내용이 갈라진다. 번호(①~⑪)로 시작하는 줄이
    항목이고, 그 아래 들여쓴 줄은 같은 항목의 내용이다. 더 깊이 들여쓴 줄은
    보충 설명이라 한 단 들여 흐리게 보인다.

    extra 는 항목 이름 → 그 칸 끝에 덧붙일 HTML (예: 시세 칸의 그래프).
    """
    extra = extra or {}
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    for line in lines:
        match = SECTION.match(line)
        if match:
            content = [(0, match.group(3))] if match.group(3) else []
            sections.append((match.group(2).strip(), content))
        elif sections and line.strip():
            indent = len(line) - len(line.lstrip(" "))
            sections[-1][1].append((indent, line.strip()))

    rows = []
    for label, content in sections:
        # 항목마다 들여쓰기 기준이 달라(4칸·8칸) 그 항목에서 가장 얕은 줄을 기준으로 삼는다
        base = min((indent for indent, _ in content if indent), default=0)
        parts = []
        for indent, text in content:
            sub = indent > base and indent != 0
            style = "padding-left:14px;color:#666;font-size:12px" if sub else ""
            parts.append(f"<div style='{style}'>{escape(text)}</div>")
        rows.append(
            f"<tr><td style='{LABEL_TD}'>{escape(label)}</td>"
            f"<td style='{VALUE_TD}'>{''.join(parts)}{extra.get(label, '')}</td></tr>"
        )
    return (
        "<table cellpadding='0' cellspacing='0' "
        "style='border-collapse:collapse;width:100%;border-top:1px solid #eee'>"
        + "".join(rows) + "</table>"
    )


def build_html(
    items: list[dict], verdicts: dict, today, degraded=None, key_of=None, images=None
) -> str:
    """보고서 본문. 그래프 PNG 는 넘겨받은 images 에 {cid: 바이트} 로 채운다."""
    images = {} if images is None else images
    key_of = key_of or (lambda i: f"{i['category']}:{i['house_manage_no']}:{i['pblanc_no']}")

    urgent = sum(1 for i in items if i.get("urgent"))
    fresh = sum(1 for i in items if i.get("is_new"))
    ratio = next(
        (v.income_ratio for v in verdicts.values() if getattr(v, "income_ratio", 0)), 0
    )

    out = [
        "<html><body style='font-family:-apple-system,\"Malgun Gothic\",sans-serif;"
        "max-width:900px;margin:0 auto;padding:16px;color:#222'>",
        f"<h2 style='margin:0 0 4px'>🏠 오늘의 청약 · {today:%Y년 %m월 %d일}</h2>",
        f"<p style='margin:0 0 16px;color:#666;font-size:14px'>"
        f"신청 가능 <b>{len(items)}건</b>"
        + (f" · 신규 {fresh}건" if fresh else "")
        + (f" · <span style='color:#c33'>마감임박 {urgent}건</span>" if urgent else "")
        + (f" · 가구 소득 {ratio:.0f}%" if ratio else "")
        + "</p>",
        # 발송 전 검증 결과는 본문이 다 만들어진 뒤에 나오므로 자리만 잡아 둔다
        CHECKS_MARK,
    ]

    if degraded:
        out.append(
            "<p style='background:#fff4e5;border-left:3px solid #f90;padding:8px 12px;"
            f"font-size:13px'>⚠️ 청약홈 API 조회 불완전: {escape(', '.join(degraded))}<br>"
            "해당 유형은 오늘 확인하지 못했습니다.</p>"
        )

    if not items:
        out.append("<p>조건에 맞는 공고가 없습니다.</p></body></html>")
        return "".join(out)

    # ── 한눈에 보는 표 ─────────────────────────────
    out.append("<table style='border-collapse:collapse;width:100%;margin-bottom:24px'><tr>")
    for head in ("#", "단지", "마감", "통근 🚌/🚗", "분양가", "시세 대비", "신청 가능"):
        out.append(f"<th style='{TH}'>{head}</th>")
    out.append("</tr>")

    for item in items:
        verdict = verdicts.get(key_of(item))
        transit = sum(v for v in (item.get("commute") or {}).values() if v < 999)
        car = sum(v for v in (item.get("commute_car") or {}).values() if v < 999)
        flag = "🔴" if item.get("urgent") else ("🆕" if item.get("is_new") else "")
        url = escape((item.get("url") or "").strip())
        name = escape(str(item.get("name", "")))
        out.append(
            f"<tr>"
            f"<td style='{TD}'>{flag}{item.get('rank', '')}</td>"
            f"<td style='{TD}'><a href='{url}' style='color:#06c;text-decoration:none'>{name}</a>"
            f"<br><span style='color:#888;font-size:11px'>{escape(str(item.get('area_name') or ''))}"
            f" · {escape(str(item.get('category')))}</span></td>"
            f"<td style='{TD}'>{_dday(item)}</td>"
            f"<td style='{TD}'>{transit or '-'} / {car or '-'}</td>"
            f"<td style='{TD}'>{detail.price_span(item) or '-'}</td>"
            f"<td style='{TD}'>{_market_cell(item)}</td>"
            f"<td style='{TD};font-size:12px'>{_routes_cell(verdict) if verdict else '-'}</td>"
            f"</tr>"
        )
    out.append("</table>")

    # ── 공고별 상세 ────────────────────────────────
    for item in items:
        verdict = verdicts.get(key_of(item))
        if not verdict:
            continue
        url = escape((item.get("url") or "").strip())
        out.append(
            "<div style='border:1px solid #e5e5e5;border-radius:6px;padding:12px 16px;"
            "margin-bottom:16px'>"
            f"<h3 style='margin:0 0 6px;font-size:15px'>"
            f"<a href='{url}' style='color:#111;text-decoration:none'>"
            f"{escape(detail.title(item))}</a></h3>"
            + link_bar(item, today)
            + detail_table(detail.body(item, verdict), {"시세": market_chart(item, images)})
            + "</div>"
        )

    advice = next(
        (a for a in (strategy.couple_advice(v) for v in verdicts.values()) if a), []
    )
    if advice:
        out.append(
            "<p style='background:#f0f6ff;border-left:3px solid #06c;padding:8px 12px;"
            "font-size:13px'>" + "<br>".join(escape(line) for line in advice) + "</p>"
        )

    out.append(
        "<p style='color:#888;font-size:12px;margin-top:20px'>"
        "※ 판정은 참고용이며 최종 확인은 모집공고문입니다.<br>"
        "※ 평수는 전용면적 기준입니다. 흔히 말하는 '34평형'은 공급면적 기준이라 더 큽니다.<br>"
        "※ 시세는 같은 시군구의 비슷한 전용면적 실거래 중위값입니다."
        "</p></body></html>"
    )
    return "".join(out)


def send(
    profile: dict, html: str, today, images: dict[str, bytes] | None = None,
    subject: str | None = None,
) -> bool:
    """설정된 주소로 보고서를 보낸다. 보냈으면 True.

    보낼지 말지는 계정 정보가 있느냐로 정한다. 설정은 세 군데(로컬 파일,
    PROFILE_YAML 시크릿, 계정 시크릿)에 나뉘어 있어, 켜는 스위치를 따로 두면
    두 군데만 고치고 왜 안 되는지 헤매기 쉽다. 끄고 싶을 때만 설정에
    enabled: false 를 적는다.
    """
    settings = (profile.get("notify") or {}).get("email") or {}
    if settings.get("enabled") is False:
        print("[건너뜀] 메일: 설정에서 notify.email.enabled 가 꺼져 있습니다.", file=sys.stderr)
        return False

    sender = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    # 구글은 앱 비밀번호를 'abcd efgh ijkl mnop' 처럼 네 자씩 띄어 보여 준다.
    # 보이는 대로 옮겨 적는 게 자연스러우므로 공백은 여기서 걷어낸다.
    password = "".join((os.environ.get("GMAIL_APP_PASSWORD") or "").split())
    missing = [
        name
        for name, value in (("GMAIL_ADDRESS", sender), ("GMAIL_APP_PASSWORD", password))
        if not value
    ]
    if missing:
        print(
            f"[건너뜀] 메일: {' / '.join(missing)} 가 비어 있습니다."
            " GitHub 저장소 Settings > Secrets and variables > Actions 에서 확인하세요.",
            file=sys.stderr,
        )
        return False

    # 받는 사람을 비워 두면 보내는 계정(운영자)에게 남의 알림이 가므로 보내지 않는다
    recipients = settings.get("to") or []
    if not recipients:
        print("[건너뜀] 메일: 프로필의 notify.email.to 가 비어 있습니다.", file=sys.stderr)
        return False
    message = EmailMessage()
    message["Subject"] = subject or f"[청약] {today:%m월 %d일} 오늘의 청약"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content("HTML 메일입니다. HTML 을 지원하는 앱에서 열어 주세요.")
    message.add_alternative(html, subtype="html")
    html_part = message.get_payload()[1]
    for cid, png in (images or {}).items():
        html_part.add_related(png, "image", "png", cid=f"<{cid}>")

    try:
        # 기본값은 PC 이름인데, 한글 이름이면 EHLO 에서 ascii 인코딩 오류가 난다
        with smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, local_hostname="localhost", timeout=TIMEOUT
        ) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        print(
            f"[실패] 메일 로그인 거부: {exc}\n"
            "       구글 계정 비밀번호가 아니라 앱 비밀번호(16자)여야 합니다.\n"
            "       https://myaccount.google.com/apppasswords 에서 다시 발급해 보세요.",
            file=sys.stderr,
        )
        return False
    except (smtplib.SMTPException, OSError) as exc:
        print(f"[실패] 메일 발송: {exc}", file=sys.stderr)
        return False

    print(f"[전송] 메일: {', '.join(recipients)}")
    return True
