"""구글 폼 응답(CSV) → profiles/<별칭>.yaml.

응답 시트 › 파일 › 다운로드 › CSV 로 받은 파일을 'NEW_USER PROFILE' 폴더에 넣고
응답반영.bat 을 더블클릭하면(또는 python -m src.import_form [파일]) 아직 프로필이
없는 사람만 골라 profiles/f01.yaml, f02.yaml … 로 만든다. 같은 사람은 알림 메일
주소로 알아보고 건너뛴다. 지인 정보는 이 PC 안에서만 처리된다.

질문 제목 앞의 [번호](docs/create-google-form.gs·google-form.md 와 같음)로 열을
찾는다. 폼은 고를 수 있는 것은 객관식·드롭다운, 금액은 만원 단위 숫자(범위 검사)로
받으므로 여기서는 보기 → 값 바꾸기가 대부분이다. 그래도 빠진 필수값 등은
'확인 필요'(⚠)로 알려 준다.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import yaml

from .config import PROFILES_DIR, ROOT

# 응답 CSV 를 넣어 두는 폴더. 지인 개인정보가 들어 있어 .gitignore 되어 있다.
INBOX = ROOT / "NEW_USER PROFILE"

MAN = 10_000  # 폼의 금액은 만원 단위
# [1-4] 크기 구간(여러 개 선택) 앞부분 → 전용면적(㎡) 범위. 평은 흔히 말하는 공급면적 평형
# (24평형 ≈ 전용 59㎡, 34평형 ≈ 전용 84㎡)을 전용으로 바꾼 값이다.
BANDS = {"20평 미만": [0, 49.99], "20평대": [50, 72.99], "30평대": [73, 99.99], "40평 이상": [100, 9999]}
# 예전 1-4(하나만 고르는 보기) 응답도 읽는다
AREAS = {"59㎡ 전후": (55, 64), "59~84㎡": (55, 86), "84㎡ 전후": (80, 86), "상관없음": (0, 200)}
# [1-6] 예산 보기 → 상한(원). 상관없음은 None
BUDGETS = {"3억 이하": 300_000_000, "3억 ~ 5억": 500_000_000, "5억 ~ 7억": 700_000_000,
           "7억 ~ 10억": 1_000_000_000}
# 연봉 구간 → 판정에 쓸 월 소득(원). 폼 보기 글자와 같아야 한다(docs/create-google-form.gs).
# 구간 경계를 청약 소득 기준선(월 소득 × 12, 2026-09 LH·청약홈 공고 기준표)에 맞췄으므로
# 구간 안의 한 값(위쪽 경계 근처)으로 판정해도 그 구간의 판정은 모두 같다.
#   가구 합산(월): 임대 신혼Ⅱ 80%(2인, 가산 전) 469.3만 · 분양 3인 이하 100% 816.8만 · 120% 980.2만
#                 · 130% 1,061.9만 · 140% 1,143.6만 · 임대 2인 200% 1,231.9만 · 160% 1,306.9만 · 200% 1,633.7만
#   본인(월): 청년 매입임대 1인 50% 266.9만 · 100% 457.6만
# ponytail: 4인 이상 가구는 분양 기준선이 달라(100% 880.2만) 구간과 어긋난다. 늘면 가구원 수별 보기로.
HOUSEHOLD_INCOME = {
    "5,600만 원 이하": 4_666_000,
    "5,600만 ~ 9,800만 원": 8_166_000,
    "9,800만 ~ 1억 1,700만 원": 9_750_000,
    "1억 1,700만 ~ 1억 2,700만 원": 10_583_000,
    "1억 2,700만 ~ 1억 3,700만 원": 11_416_000,
    "1억 3,700만 ~ 1억 4,700만 원": 12_250_000,
    "1억 4,700만 ~ 1억 5,600만 원": 13_000_000,
    "1억 5,600만 ~ 1억 9,600만 원": 16_333_000,
    "1억 9,600만 원 초과": 17_500_000,
}
# 구간의 (최소, 최대) 월 소득. 임대 유형마다 기준선이 달라, 구간이 기준선에 걸치면 '확인 필요'로 판정한다
# (lease_rules._cap). 경계는 보기 글자의 연봉 ÷ 12.
_EDGES = [0, 5600, 9800, 11700, 12700, 13700, 14700, 15600, 19600, 10 ** 9]  # 만원/년
HOUSEHOLD_RANGE = {label: [round(_EDGES[i] * MAN / 12), round(_EDGES[i + 1] * MAN / 12)]
                   for i, label in enumerate(HOUSEHOLD_INCOME)}
PERSON_INCOME = {
    "소득 없음": 0,
    "3,200만 원 이하": 2_666_000,
    "3,200만 ~ 5,400만 원": 4_500_000,
    "5,400만 원 초과": 6_000_000,
}
PERSON_RANGE = {
    "소득 없음": [0, 0],
    "3,200만 원 이하": [0, round(3200 * MAN / 12)],
    "3,200만 ~ 5,400만 원": [round(3200 * MAN / 12), round(5400 * MAN / 12)],
    "5,400만 원 초과": [round(5400 * MAN / 12), 10 ** 12],
}
# 공고 주소에 들어 있는지로 거르므로, 다른 구 이름에 섞이는 '동구'(남동구)는 앞에 빈칸을 둔다
INCHEON_TOKEN = {"동구": " 동구"}


def yes(value: str) -> bool | None:
    """'예' / '아니요' / '예 – 집 없음(무주택)' 처럼 뜻풀이가 붙은 보기도 앞말로 판단한다."""
    return {"예": True, "아니요": False}.get((value or "").split("–")[0].strip())


def man(value: str, issues: list[str], label: str) -> int | None:
    """만원 단위 숫자 → 원. 폼에서 숫자·범위를 검사하므로 실패는 드물다."""
    text = (value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return round(float(text) * MAN)
    except ValueError:
        issues.append(f"{label}: 숫자가 아님 ('{value}') — 원 단위로 직접 고치세요")
        return None


def num(value: str) -> int | None:
    match = re.match(r"\s*(\d+)", value or "")
    return int(match.group(1)) if match else None


def day(value: str) -> str | None:
    """시트 날짜('1995. 3. 15', '1995-03-15', '3/15/1995') → 'YYYY-MM-DD'."""
    text = (value or "").strip()
    if match := re.match(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text):
        y, m, d = match.groups()
    elif match := re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text):
        m, d, y = match.groups()
    else:
        return None
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def picks(value: str) -> list[str]:
    """체크박스 응답 '서울, 경기' → ['서울', '경기']."""
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def person(row: dict[str, str], me: bool, issues: list[str]) -> dict:
    """본인(me)·배우자 공통 항목. 배우자는 번호가 조금 다르다."""
    k = (lambda mine, theirs: row.get(mine if me else theirs, ""))
    region = k("3-2", "5-2").strip() or None
    district = k("3-2b", "5-2b").strip() or None
    workplace = k("2-1", "2-2").strip()
    out = {
        "label": "본인" if me else "배우자",
        "birth_date": day(k("3-1", "5-1")),
        "monthly_income": PERSON_INCOME.get(k("3-8", "5-5").strip()),
        "income_range": PERSON_RANGE.get(k("3-8", "5-5").strip()),
        "address": " ".join(x for x in (region, district) if x) or None,
        "region": region,
        "district": district,
        "homeless": yes(k("3-5", "5-3")),  # 본인(배우자는 그 세대원 포함) 무주택
        "ever_owned_home": yes(k("3-7", "5-4")),
        "income_tax_years": num(k("3-10", "5-7")),
        "parents_homeless": yes(k("3-11", "5-8")),
        "account": {
            "type": "주택청약종합저축",
            "opened_date": day(k("3-12", "5-9a")),
            "payment_count": num(k("3-13", "5-9b")),
            "balance": man(k("3-14", "5-9c"), issues, "통장 납입 금액"),
        },
        "assets": {
            "real_estate": man(k("3-16", "5-10a"), issues, "부동산 가액"),
            "car": man(k("3-17", "5-10b"), issues, "자동차 가액"),
        },
        "workplace": {"name": workplace, "query": workplace} if workplace else {"name": None, "query": None},
    }
    if me:
        out.update({
            "moved_in_date": day(row.get("3-3", "")),
            "is_householder": yes(row.get("3-4", "")),
            "homeless_since": day(row.get("3-6", "")),
        })
        out["account"]["monthly_payment"] = man(row.get("3-15", ""), issues, "월 납입액")
    return out


def profile_from(row: dict[str, str]) -> tuple[dict, list[str]]:
    """응답 한 줄 → (프로필, 확인 필요 목록). row 의 키는 질문 번호('3-8')."""
    issues: list[str] = []
    get = row.get

    regions = picks(get("1-1", ""))
    cities = {}
    # 경기·인천은 1-2·1-3 에서 '전체' 또는 시·군·구를 고른다. '전체'가 있으면 시·군은 무시한다.
    for region, key in (("경기", "1-2"), ("인천", "1-3")):
        chosen = picks(get(key, ""))
        if f"{region} 전체" in chosen:
            regions.append(region)
        elif chosen and region not in regions:
            names = [re.sub(r"\(.*\)", "", c).strip() for c in chosen]
            cities[region] = [INCHEON_TOKEN.get(n, n) if region == "인천" else n for n in names]
    if not regions and not cities:
        issues.append("희망 지역(1-1·1-2·1-3)이 비어 있음 — 이대로면 공고를 하나도 못 받음")
    # 비우면 폼 안내대로 20평대·30평대
    answer = get("1-4", "")
    ranges = [band for key, band in BANDS.items() for chosen in picks(answer) if chosen.startswith(key)]
    old = next((v for key, v in AREAS.items() if answer.startswith(key)), None)
    if old:
        ranges = [list(old)]
    if not ranges and answer.strip():
        issues.append(f"[1-4] 크기 보기를 못 알아봄 ('{answer}') — area_ranges 를 직접 채우세요")
    ranges = ranges or [BANDS["20평대"], BANDS["30평대"]]
    area = (min(r[0] for r in ranges), max(r[1] for r in ranges))

    profile = {"applicant": person(row, True, issues)}
    status = get("4-1", "").strip()  # 비우면 폼 안내대로 미혼
    if not status and get("5-1", "") + get("5-5", "") + get("5-2", ""):
        issues.append("결혼 상태(4-1)를 비웠는데 배우자 정보가 있음 — 미혼으로 판정했으니 확인하세요")
    married = status.startswith("기혼")
    engaged = status.startswith("결혼 예정")
    if yes(get("3-5b", "")):
        profile["applicant"]["household_home"] = {"owner": "가족", "separated": False}
    has_spouse = married or engaged
    if has_spouse:
        spouse = person(row, False, issues)
        if yes(get("5-11", "")):
            spouse["household_home"] = {
                "owner": get("5-12a", "").strip() or "가족",
                "location": get("5-12b", "").strip() or None,
                "area_max_60": yes(get("5-12c", "")),
                "price": None,
                "separated": bool(yes(get("5-12d", ""))),
            }
        profile["spouse"] = spouse
    profile["household"] = {
        "marriage_registered": married,
        "marriage_date": day(get("4-3", "")),
        # 혼인신고 시점 조절은 결혼 예정일 때만 의미가 있다
        "marriage_flexible": yes(get("4-4", "")) if engaged else None,
        "parents_dependents": num(get("4-7", "")) or 0,
        # 가구 합산 월 소득(구간 대표값). 있으면 사람별 소득을 더하는 대신 이 값으로 판정한다
        "monthly_income": HOUSEHOLD_INCOME.get(get("3-9", "").strip()),
        "income_range": HOUSEHOLD_RANGE.get(get("3-9", "").strip()),
        "children": num(get("4-5", "")) or 0,
        "household_size": num(get("4-6", "")) or (2 if has_spouse else 1),
    }
    profile["filters"] = {
        "regions": regions,
        "city_regions": sorted(cities),
        "cities": cities,
        "area_min": float(area[0]),
        "area_max": float(area[1]),
        "area_ranges": ranges,  # 고른 구간들. 떨어진 구간(20평 미만 + 30평대)도 각각 본다
        "apartment_only": bool(yes(get("1-5", ""))),
        "exclude_house_types": [],
        "price_cap": None,
        "budget": BUDGETS.get(get("1-6", "").strip()),
        "lookback_days": 60,
    }
    to = [e.strip() for e in (get("0-2", ""), get("0-2b", "")) if e.strip()]
    if not to:
        issues.append("알림 메일 주소가 없음")
    profile["notify"] = {"email": {"to": to}}
    # 아래는 지인이 일부러 비웠을 수 있는 선택 항목이라 '참고'로만 알린다 (고치지 않아도 알림은 감)
    for empty, note in (
        (not profile["applicant"]["birth_date"], "생년월일"),
        (not profile["applicant"]["region"], "주민등록 시·도"),
        (profile["household"]["monthly_income"] is None, "가구 합산 연봉"),
    ):
        if empty:
            issues.append(f"(참고) {note} 비워 둠 — 관련 판정은 '확인 필요'로 나감. 그대로 둬도 됨")
    return profile, issues


def read_rows(path: Path) -> list[dict[str, str]]:
    """CSV → 질문 번호를 키로 하는 dict 들. 동의하지 않은 응답은 뺀다."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        keys = [(m.group(1) if (m := re.match(r"\s*\[([0-9a-z-]+)\]", h)) else h) for h in header]
        rows = []
        for line in reader:
            if not any(cell.strip() for cell in line):
                continue
            # 폼을 다시 만들면 같은 번호 칸이 여러 벌 생긴다. 답이 있는 칸을 쓴다.
            row: dict[str, str] = {}
            for key, cell in zip(keys, line):
                if cell.strip() or key not in row:
                    row[key] = cell
            rows.append(row)
    return [r for r in rows if "동의" in r.get("0-1", "")]


def existing_emails(folder: Path) -> set[str]:
    emails = set()
    for file in folder.glob("*.yaml"):
        data = yaml.safe_load(file.read_text(encoding="utf-8-sig")) or {}
        emails.update(e.lower() for e in ((data.get("notify") or {}).get("email") or {}).get("to") or [])
    return emails


def next_alias(folder: Path) -> str:
    used = {int(m.group(1)) for f in folder.glob("f*.yaml") if (m := re.fullmatch(r"f(\d+)", f.stem))}
    return f"f{max(used, default=0) + 1:02d}"


def latest_download() -> Path | None:
    """INBOX 에서 가장 최근의 이 폼 응답 CSV (첫 줄에 '[0-1]' 질문이 있는 파일)."""
    INBOX.mkdir(exist_ok=True)
    for path in sorted(INBOX.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        with path.open(encoding="utf-8-sig", errors="ignore") as f:
            if "[0-1]" in f.readline():
                return path
    return None


def main() -> int:
    # 파일을 주지 않으면 'NEW_USER PROFILE' 폴더의 최신 응답 CSV 를 쓴다 (응답반영.bat 더블클릭용)
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_download()
    if not path:
        print(f"{INBOX} 폴더에 응답 CSV 가 없습니다.\n"
              "응답 시트 › 파일 › 다운로드 › CSV 로 받아 이 폴더에 넣어 주세요.")
        return 1
    print(f"읽는 파일: {path.name}\n")
    rows = read_rows(path)
    known = existing_emails(PROFILES_DIR)
    made = 0
    for row in rows:
        profile, issues = profile_from(row)
        email = (profile["notify"]["email"]["to"] or [""])[0].lower()
        if email and email in known:
            continue
        alias = next_alias(PROFILES_DIR)
        path = PROFILES_DIR / f"{alias}.yaml"
        path.write_text(
            f"# 구글 폼 응답에서 만듦 (제출 {row.get('타임스탬프', '')}). 항목 설명은 example.yaml\n"
            + yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        known.add(email)
        made += 1
        print(f"✔ {alias}  {email}")
        for issue in issues:
            print(f"    ⚠ {issue}")
    print(f"\n새로 만든 프로필 {made}건 (응답 {len(rows)}건 중, 이미 있는 사람은 건너뜀)")
    if made:
        print("⚠ 가 있으면 profiles 폴더의 그 파일을 메모장으로 고쳐 저장한 뒤 응답반영.bat 을 한 번 더 실행하세요.\n"
              "  (이미 만든 사람은 건너뛰고 시크릿에 넣을 내용만 새로 만듭니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
