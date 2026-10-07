"""시공사와 브랜드.

대형 건설사 브랜드 단지는 나중에 팔기 쉽고 시세도 받쳐 주는 편이라 투자 판단에
참고가 된다. 청약홈 APT 공고에는 시공사(CNSTRCT_ENTRPS_NM)가 있지만 무순위·
임의공급 공고에는 비어 있어, 단지 이름 속 브랜드로 시공사를 가늠한다.

TOP10 은 국토교통부 시공능력평가 상위 10개사(2025년 기준)다. 매년 7월 말 새
순위가 발표되면 갱신한다.
"""

from __future__ import annotations

# 브랜드 → 건설사. 이름에서 찾을 때 긴 것부터 맞춰야 '힐스테이트'가 '디에이치'
# 같은 다른 브랜드와 섞이지 않는다.
BRANDS = {
    "래미안": "삼성물산",
    "디에이치": "현대건설",
    "힐스테이트": "현대건설·현대엔지니어링",
    "푸르지오": "대우건설",
    "써밋": "대우건설",
    "자이": "GS건설",
    "더샵": "포스코이앤씨",
    "오티에르": "포스코이앤씨",
    "롯데캐슬": "롯데건설",
    "르엘": "롯데건설",
    "e편한세상": "DL이앤씨",
    "아크로": "DL이앤씨",
    "아이파크": "HDC현대산업개발",
    "SK뷰": "SK에코플랜트",
    "SK VIEW": "SK에코플랜트",
    "드파인": "SK에코플랜트",
    "호반써밋": "호반건설",
    "한화포레나": "한화 건설부문",
    "포레나": "한화 건설부문",
    "더플래티넘": "쌍용건설",
    "센트레빌": "동부건설",
    "하늘채": "코오롱글로벌",
    "리슈빌": "계룡건설",
    "어울림": "금호건설",
    "데시앙": "태영건설",
    "로얄듀크": "동양건설산업",
}

# 공고에는 'DL이앤씨'와 '디엘이앤씨'처럼 표기가 섞여 오므로 둘 다 둔다
TOP10 = (
    "삼성물산", "현대건설", "대우건설", "현대엔지니어링",
    "GS건설", "지에스건설", "포스코이앤씨", "롯데건설",
    "SK에코플랜트", "에스케이에코플랜트",
    "HDC현대산업개발", "에이치디씨현대산업개발", "DL이앤씨", "디엘이앤씨",
)


def brand(name: str) -> str:
    """단지 이름 속 브랜드. 없으면 빈 문자열."""
    for word in sorted(BRANDS, key=len, reverse=True):
        if word.lower() in (name or "").lower():
            return word
    return ""


def summarize(item: dict) -> dict:
    name = str(item.get("name") or "")
    found = brand(name)
    builder = str(item.get("builder") or "").strip()
    guessed = False
    if not builder and found:
        builder, guessed = BRANDS[found], True
    # 공동 시공은 '(주)시티건설, (주)시티종합건설' 처럼 쉼표로 이어져 온다
    top10 = any(company in builder.replace("(주)", "").replace(" ", "")
                for company in TOP10)
    return {
        "builder": builder,
        "guessed": guessed,
        "brand": found,
        "top10": top10,
        "units": str(item.get("total_units") or "").strip(),
    }


def enrich(items: list[dict]) -> None:
    for item in items:
        item["builder_info"] = summarize(item)
