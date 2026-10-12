"""블로그 대표 이미지(썸네일) 카드.

네이버는 글의 첫 이미지를 검색 결과 썸네일로 쓴다. 인물·공고 수·대표 단지를 큰 글씨로
담은 정사각형 PNG 를 만든다. 한글 글꼴이 없으면(GitHub 서버에 fonts-nanum 미설치 등)
네모로 깨지므로 아예 만들지 않는다.
"""

from __future__ import annotations

import io

from matplotlib import font_manager
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

FONTS = ("NanumGothic", "Malgun Gothic", "AppleGothic", "Noto Sans CJK KR")
NAVY = "#1f3b5c"
ACCENT = "#f2b134"


def korean_font() -> str | None:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONTS if f in installed), None)


def card_png(label: str, headline: str, top: str, sub: str, footer: str) -> bytes | None:
    """1080×1080 카드. 글꼴이 없으면 None."""
    font = korean_font()
    if not font:
        return None
    fig = Figure(figsize=(7.2, 7.2), dpi=150)
    fig.patch.set_facecolor("white")
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.add_patch(_rect(0, 0.72, 1, 0.28, NAVY))
    ax.add_patch(_rect(0.08, 0.705, 0.18, 0.03, ACCENT))
    style = {"fontfamily": font, "ha": "left"}
    ax.text(0.08, 0.86, label, color="white", fontsize=30, fontweight="bold", va="center", **style)
    ax.text(0.08, 0.58, headline, color=NAVY, fontsize=34, fontweight="bold", va="center", **style)
    ax.text(0.08, 0.42, _fit(top, 15), color="#222", fontsize=24, va="center", **style)
    ax.text(0.08, 0.32, _fit(sub, 22), color="#555", fontsize=18, va="center", **style)
    ax.text(0.08, 0.08, footer, color="#888", fontsize=13, va="center", **style)
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def _rect(x, y, w, h, color):
    return Rectangle((x, y), w, h, facecolor=color, edgecolor="none")


def _fit(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


if __name__ == "__main__":
    png = card_png("신혼 맞벌이", "새 청약 공고 7건", "산곡역자이힐스테이트앤하늘채",
                   "신혼부부 특공 · D-9 마감", "10월 12일 · 가상 인물 맞춤 청약 정리")
    assert png is None or png[:4] == b"\x89PNG"
    print("ok" if png else "ok (한글 글꼴 없음 → 썸네일 생략)")
