"""발송 전 2단계 검증.

1차는 수집·가공한 데이터를, 2차는 완성된 메일 내용을 본다. 문제가
있어도 발송은 멈추지 않는다. 마감이 임박한 공고를 놓치는 것이 더 큰 손해라,
대신 알림 맨 위에 '확인 필요'로 적어 받는 사람이 직접 보게 한다.

규칙은 '틀리면 판단을 그르칠 값'을 고른 것이다. 외부 자료(청약홈·실거래·
공고문 PDF)를 여러 군데서 모아 붙이므로, 한 군데가 형식을 바꾸면 조용히
엉뚱한 숫자가 나갈 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import escape

from .applyhome import area_ok

# 분양가가 이 범위를 벗어나면 단위(만원/원) 해석이 틀렸을 가능성이 크다
PRICE_RANGE = (50_000_000, 10_000_000_000)
# 시세·주변 분양가와 이만큼 넘게 차이 나면 비교군이 잘못 잡혔을 수 있다
GAP_LIMIT = 80
COMMUTE_RANGE = (1, 300)
# 완성된 문구에 이런 조각이 보이면 값이 비어 그대로 찍힌 것이다
# 한글은 \b 의 단어 문자라 'None억' 에서 경계가 생기지 않는다. 영문자만 경계로 본다.
LEFTOVERS = re.compile(
    r"(?<![A-Za-z])(?:None|nan|NaN)(?![A-Za-z])|\{[a-z_]+\}|(?<![\d.])0\.0억"
)


@dataclass
class Issue:
    stage: int
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.stage}차] {self.where}: {self.message}"


def _name(item: dict) -> str:
    return str(item.get("name") or "(이름 없음)")[:20]


def check_data(items: list[dict], verdicts: dict, key_of, today: date, filters: dict) -> list[Issue]:
    """1차: 공고 데이터 자체가 말이 되는지."""
    issues: list[Issue] = []
    seen: set[str] = set()

    for item in items:
        where = _name(item)

        def add(message: str) -> None:
            issues.append(Issue(1, where, message))

        key = key_of(item)
        if key in seen:
            add("같은 공고가 두 번 들어감")
        seen.add(key)

        if not str(item.get("url") or "").startswith("http"):
            add("공고 링크 없음")

        ends = [d for d in (item.get("special_end"), item.get("general_end")) if d]
        if not ends:
            add("접수 마감일 없음")
        elif max(ends) < today:
            add(f"이미 마감된 공고가 섞임 ({max(ends):%m.%d})")
        for label in ("special", "general"):
            begin, end = item.get(f"{label}_begin"), item.get(f"{label}_end")
            if begin and end and begin > end:
                add(f"접수 시작일이 마감일보다 늦음 ({begin:%m.%d} > {end:%m.%d})")
        announce = item.get("announce_date")
        if announce and ends and announce < max(ends):
            add(f"당첨자 발표일({announce:%m.%d})이 접수 마감보다 이름")

        low, high = item.get("price_min") or 0, item.get("price_max") or 0
        if low and not PRICE_RANGE[0] <= low <= PRICE_RANGE[1]:
            add(f"분양가가 비정상 범위 ({low / 1e8:.2f}억)")
        if low and high and low > high:
            add("최저 분양가가 최고 분양가보다 큼")

        for model in item.get("matched_models") or []:
            area = model.get("area") or 0
            if area and not area_ok(area, filters):
                add(f"면적 조건 밖 주택형이 섞임 ({area:g}㎡)")
                break

        market = item.get("market") or {}
        if market:
            if not market.get("median"):
                add("주변 시세 중위값이 0")
            if abs(market.get("gap_pct") or 0) > GAP_LIMIT and item.get("category") == "APT":
                add(f"시세 대비 차이가 너무 큼 ({market['gap_pct']:+.0f}%) — 비교군 확인")
        presales = item.get("presales") or {}
        if presales and abs(presales.get("gap_pct") or 0) > GAP_LIMIT:
            add(f"주변 분양가 대비 차이가 너무 큼 ({presales['gap_pct']:+.0f}%)")

        units = item.get("complex_units") or 0
        try:
            this_time = int(str(item.get("total_units") or "0").replace(",", ""))
        except ValueError:
            this_time = 0
        if units and this_time and units < this_time:
            add(f"전체 세대수({units})가 이번 공급({this_time})보다 적음 — 공고문 읽기 오류")

        for place, minutes in (item.get("commute") or {}).items():
            if minutes < 999 and not COMMUTE_RANGE[0] <= minutes <= COMMUTE_RANGE[1]:
                add(f"통근시간 이상 ({place} {minutes}분)")

        regulation = item.get("regulation") or {}
        until = regulation.get("resale_until")
        if until and announce and until <= announce:
            add("전매제한 종료일이 발표일보다 이름")

        verdict = verdicts.get(key)
        if verdict is None:
            add("자격 판정 결과 없음")
        elif not verdict.eligible:
            add("신청 가능한 방법이 하나도 없는데 목록에 들어감")
    return issues


def check_leases(leases: list[dict], today: date) -> list[Issue]:
    """임대 1차: 수집한 LH 임대 공고가 말이 되는지."""
    issues: list[Issue] = []
    seen: set[str] = set()
    for lease in leases:
        where = str(lease.get("name") or "(이름 없음)")[:20]
        if lease.get("name") in seen:
            issues.append(Issue(1, where, "같은 공고가 두 번 들어감"))
        seen.add(lease.get("name"))
        if not str(lease.get("url") or "").startswith("http"):
            issues.append(Issue(1, where, "공고 링크 없음"))
        if not lease.get("kind"):
            issues.append(Issue(1, where, "신청 가능한 유형이 아닌데 들어감"))
        deadline = lease.get("deadline")
        if not deadline or deadline < today:
            issues.append(Issue(1, where, "마감됐거나 마감일 없음"))
        # 단지형(국민·행복 등)은 주택목록 첨부가 원래 없다
        if "top" in lease and not lease["top"] and not lease.get("complex"):
            issues.append(Issue(1, where, "주택목록·통근시간을 구하지 못함"))
        elif any(m >= 999 for b in lease.get("top") or [] if b.get("commute_car")
                 for m in (b.get("commute") or {"": 999}).values()):
            issues.append(Issue(1, where, "대중교통 시간 일부 없음(ODsay)"))
        if "announce" in lease and not lease["announce"] and not lease.get("complex"):  # 단지형은 일정 표기가 제각각
            issues.append(Issue(1, where, "공고문에서 발표일을 못 읽음"))
        elif lease.get("announce") and deadline and lease["announce"] <= deadline:
            issues.append(Issue(1, where, "발표일이 접수 마감보다 빠름"))
    return issues


# ── 맞춤법 ────────────────────────────────────────────
# 한국어 맞춤법 검사기는 공식 API 가 없다(네이버·부산대 검사기를 프로그램으로 부르는 건 비공식이라 자주 깨진다).
# 그래서 프로그램이 만드는 문장에서 실제로 틀릴 수 있는 것만 규칙으로 본다: 변수 뒤 조사, 반복 단어, 공백, 흔한 오기.
JOSA = {"으로": ("으로", "로"), "로": ("으로", "로"), "은": ("은", "는"), "는": ("은", "는"),
        "이": ("이", "가"), "가": ("이", "가"), "을": ("을", "를"), "를": ("을", "를"),
        "과": ("과", "와"), "와": ("과", "와")}
DIGIT_BATCHIM = {"0": 21, "1": 8, "2": 0, "3": 16, "4": 0, "5": 0, "6": 1, "7": 8, "8": 8, "9": 0}  # 영 일 이 삼 …
TYPOS = {"됬": "됐", "몇일": "며칠", "할께": "할게", "되요": "돼요", "안되요": "안 돼요", "어떻해": "어떡해",
         "왠만": "웬만", "금새": "금세", "역활": "역할", "뵈요": "봬요", "희안": "희한", "설겆이": "설거지"}


def _batchim(word: str) -> int | None:
    """단어 마지막 소리의 받침 번호(0 = 없음, 8 = ㄹ). 괄호 속은 빼고 본다. 모르면 None."""
    word = re.sub(r"\([^)]*\)\s*$", "", word).rstrip(" ·")
    if not word:
        return None
    last = word[-1]
    if "가" <= last <= "힣":
        return (ord(last) - 0xAC00) % 28
    return DIGIT_BATCHIM.get(last)


def josa(word: str, particle: str) -> str:
    """word 에 맞는 조사. josa('분당센트로', '으로') → '로', josa('5.6억', '으로') → '으로'."""
    with_b, without_b = JOSA[particle]
    b = _batchim(word)
    if b is None:
        return without_b
    if with_b == "으로":
        return "로" if b in (0, 8) else "으로"
    return with_b if b else without_b


def check_text(html: str) -> list[str]:
    """완성된 글에서 맞춤법·표기 문제를 찾는다. 찾은 문제 설명 목록."""
    problems = []
    # 1) 변수(굵은 글씨·괄호) 바로 뒤 조사: '센트로</b>(…)으로' 처럼 값이 바뀌는 자리만 본다
    for m in re.finditer(r"([가-힣0-9]+)(?:</b>)?(\([^)]*\))?(?:</b>)?(으로|로|은|는|이|가|을|를|과|와)(?=[\s,.!?<])",
                         html):
        if not (m.group(2) or "</b>" in m.group(0)):
            continue
        want = josa(m.group(1), m.group(3))
        if want != m.group(3):
            problems.append(f"조사 '{m.group(1)}{m.group(3)}' → '{m.group(1)}{want}'")
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;", " ", text)
    # 2) 같은 단어가 붙어 반복 ('오피스텔 오피스텔')
    for m in re.finditer(r"(?<![가-힣\w])([가-힣]{2,}) \1(?![가-힣\w])", text):
        problems.append(f"같은 단어 반복 '{m.group(0)}'")
    # 3) 문장부호 앞 공백·빈 괄호
    for m in re.finditer(r"[가-힣\w] +[,.?!](?=\s|$)", text):
        problems.append(f"문장부호 앞 공백 '{m.group(0).strip()}'")
    if "()" in text:
        problems.append("빈 괄호 '()'")
    # 4) 흔한 오기
    for wrong, right in TYPOS.items():
        if wrong in text:
            problems.append(f"맞춤법 '{wrong}' → '{right}'")
    return list(dict.fromkeys(problems))


def check_output(items: list[dict], html: str, images: dict[str, bytes]) -> list[Issue]:
    """2차: 받는 사람이 보게 될 메일 완성본이 온전한지."""
    issues: list[Issue] = []
    referenced = set(re.findall(r"cid:([^'\"]+)", html))
    if referenced - set(images):
        issues.append(Issue(2, "메일", f"그래프 {len(referenced - set(images))}개가 첨부에 없음"))
    for item in items:
        if escape(str(item.get("name") or "")) not in html:
            issues.append(Issue(2, "메일", f"'{_name(item)}' 공고가 메일에 없음"))
    found = LEFTOVERS.search(re.sub(r"<[^>]+>", " ", html))
    if found:
        issues.append(Issue(2, "메일", f"값이 빈 채로 찍힘 ('{found.group(0)}')"))
    issues += [Issue(2, "맞춤법", p) for p in check_text(html)]
    return issues


def email_notice(issues: list[Issue]) -> str:
    """메일 맨 위에 넣을 상자."""
    if not issues:
        return (
            "<p style='background:#eefaf3;border-left:3px solid #0a7;padding:8px 12px;"
            "font-size:13px;margin:0 0 16px'>✅ 발송 전 검증 2단계 통과 "
            "(1차 데이터 점검 · 2차 메시지 점검)</p>"
        )
    rows = "".join(f"<li>{escape(str(issue))}</li>" for issue in issues)
    return (
        "<div style='background:#fff4e5;border-left:3px solid #f90;padding:8px 12px;"
        "font-size:13px;margin:0 0 16px'>"
        f"<b>⚠️ 발송 전 검증: 확인 필요 {len(issues)}건</b>"
        f"<ul style='margin:6px 0 0;padding-left:18px'>{rows}</ul>"
        "<div style='color:#888;font-size:12px;margin-top:4px'>"
        "알림은 그대로 보냈습니다. 해당 공고는 청약홈 공고문으로 한 번 더 확인하세요.</div></div>"
    )


if __name__ == "__main__":
    assert josa("분당센트로", "으로") == "로" and josa("5.6억", "으로") == "으로" and josa("부평구 산곡동", "으로") == "으로"
    assert josa("서울", "으로") == "로" and josa("D-1", "으로") == "로" and josa("A17블록", "은") == "은"
    assert josa("더샵 분당센트로(경기 · D-1 마감)", "으로") == "로"
    assert josa("공고", "을") == "를" and josa("단지", "이") == "가" and josa("청약", "과") == "과"
    bad = check_text("가장 먼저 볼 곳은 <b>더샵 분당센트로</b>(경기 · D-1 마감)으로, 오피스텔 오피스텔 청약 . 됬다")
    assert any("분당센트로으로" in p for p in bad), bad
    assert any("반복" in p for p in bad) and any("문장부호" in p for p in bad) and any("됬" in p for p in bad)
    ok = check_text("가장 먼저 볼 곳은 <b>산곡역자이</b>(인천 · D-9 마감)로, 신혼부부 특공 자격으로 넣을 수 있습니다. 사이가 좋다.")
    assert ok == [], ok
    print("ok")
