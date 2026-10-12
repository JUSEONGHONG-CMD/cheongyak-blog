"""블로그 대표 이미지(썸네일) 카드.

네이버는 글의 첫 이미지를 검색 결과 썸네일로 쓴다. 사용자가 고른 시안(2026-10-12)을 따른다.

    ┌──────────────────────────────────┐
    │ (인물) 모델 03          10월 12일 기준 │  파란 띠
    │        맞벌이 신혼부부                 │
    ├──────────────────────────────────┤
    │ 이 청약                              │
    │ 넣을 수 있어요                        │  큰 제목
    │ ☑ 무주택 세대구성원        ( 신청 )   │
    │ ☑ 혼인 7년 이내            ( 가능 )   │  조건 3줄 + 도장
    │ ☑ 청약통장 6개월 이상                 │
    ├──────────────────────────────────┤
    │ ○○지구 신혼희망타운              D-3 │  검은 띠
    └──────────────────────────────────┘

한글 글꼴이 없으면(GitHub 서버에 fonts-nanum 미설치 등) 네모로 깨지므로 아예 만들지 않는다.
"""

from __future__ import annotations

import io

from matplotlib import font_manager, patheffects
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle, Wedge

FONTS = ("NanumGothic", "Malgun Gothic", "AppleGothic", "Noto Sans CJK KR")
HEAVY = ("NanumGothic ExtraBold", "NanumSquare ExtraBold")  # 있으면 큰 제목에 쓴다
BLUE = "#3168f5"
BLUE_SOFT = "#cddcff"
INK = "#15171c"
RED = "#e8434b"
YELLOW = "#ffd21f"
SKIN, HAIR = "#f6cfa8", "#2b2522"


def korean_font() -> str | None:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONTS if f in installed), None)


def _heavy_font(default: str) -> str:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in HEAVY if f in installed), default)


def _people(ax, adults: int, kids: int, senior: bool) -> None:
    """띠 왼쪽의 단순한 인물 아이콘. 어른은 크게, 아이는 작게 겹쳐 세운다."""
    hair = "#9a9a9a" if senior else HAIR
    shirts = ["#f5a623", INK, "#7ed3a4", "#ff8fa3"]
    x = 0.065
    for i in range(adults + kids):
        small = i >= adults
        r = 0.022 if small else 0.03
        base = 0.835
        body_h = 0.045 if small else 0.06
        ax.add_patch(FancyBboxPatch((x - r * 1.25, base), r * 2.5, body_h, boxstyle="round,pad=0,rounding_size=0.02",
                                    facecolor=shirts[i % len(shirts)], edgecolor="none", zorder=3 + i))
        cy = base + body_h + r * 0.9
        ax.add_patch(Circle((x, cy), r, facecolor=SKIN, edgecolor="none", zorder=4 + i))
        ax.add_patch(Wedge((x, cy + r * 0.35), r * 1.0, 10, 170, facecolor=hair, edgecolor="none", zorder=5 + i))
        for side in (-1, 1):  # 눈
            ax.add_patch(Circle((x + side * r * 0.38, cy - r * 0.05), r * 0.11, facecolor=INK, zorder=6 + i))
        ax.add_patch(Wedge((x, cy - r * 0.3), r * 0.28, 200, 340, width=r * 0.08, facecolor="#c0504d",
                           edgecolor="none", zorder=6 + i))  # 입
        x += 0.045 if not small else 0.035


def card_png(model_no: int, label: str, date_line: str, checks: list[str], stamp: str,
             name: str, dday: str, adults: int = 1, kids: int = 0, senior: bool = False) -> bytes | None:
    """1080×1080 카드. 글꼴이 없으면 None."""
    font = korean_font()
    if not font:
        return None
    heavy = _heavy_font(font)
    fig = Figure(figsize=(7.2, 7.2), dpi=150)
    FigureCanvasAgg(fig)  # 글자 폭을 재려면 실제 렌더러가 붙어 있어야 한다
    fig.patch.set_facecolor("white")
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    renderer = fig.canvas.get_renderer()

    def write(x, y, text, size, color, weight="bold", family=None, ha="left", limit=0.95, thick=0, **kw):
        t = ax.text(x, y, text, color=color, fontsize=size, fontfamily=family or font, ha=ha, va="center",
                    fontweight=weight, zorder=10, **kw)
        if thick:  # 굵은 글꼴이 없을 때 같은 색 외곽선으로 시안의 두꺼운 제목에 가깝게 한다
            t.set_path_effects([patheffects.withStroke(linewidth=thick, foreground=color)])
        # 단지명·조건은 길이가 제각각이라, 정해진 폭을 넘으면 넘지 않을 때까지 줄인다
        while size > 9 and t.get_window_extent(renderer).x1 > limit * fig.bbox.width:
            size -= 1
            t.set_fontsize(size)
        return t

    # 파란 띠: 인물 아이콘 · 모델 번호 · 인물 이름 · 기준일
    ax.add_patch(Rectangle((0, 0.82), 1, 0.18, facecolor=BLUE, edgecolor="none"))
    _people(ax, adults, kids, senior)
    left = 0.065 + 0.045 * adults + 0.035 * kids + 0.03
    write(left, 0.945, f"모델 {model_no:02d}", 14, BLUE_SOFT, weight="normal")
    write(left, 0.885, label, 25, "white", limit=0.72)
    write(0.95, 0.945, date_line, 13, BLUE_SOFT, weight="normal", ha="right")

    # 큰 제목
    bold = 0 if heavy != font else 3
    write(0.05, 0.705, "이 청약", 54, INK, weight="heavy", family=heavy, thick=bold)
    write(0.05, 0.565, "넣을 수 있어요", 54, INK, weight="heavy", family=heavy, thick=bold)

    # 조건 3줄: 파란 체크 상자 + 문구 (도장 자리까지)
    for i, text in enumerate(checks[:3]):
        y = 0.405 - i * 0.085
        ax.add_patch(FancyBboxPatch((0.05, y - 0.024), 0.048, 0.048, boxstyle="round,pad=0,rounding_size=0.008",
                                    facecolor=BLUE, edgecolor="none", zorder=5))
        ax.plot([0.062, 0.072, 0.088], [y, y - 0.011, y + 0.011], color="white", linewidth=3.2,
                solid_capstyle="round", solid_joinstyle="round", zorder=6)
        write(0.12, y, text, 22, INK, limit=0.64)

    # 도장
    cx, cy = 0.8, 0.325
    ax.add_patch(Circle((cx, cy), 0.115, facecolor="none", edgecolor=RED, linewidth=7, zorder=7))
    ax.add_patch(Circle((cx, cy), 0.088, facecolor="none", edgecolor=RED, linewidth=2.5, zorder=7))
    word = stamp.replace(" ", "\n", 1)
    ax.text(cx, cy, word, color=RED, fontsize=27, fontfamily=heavy, fontweight="heavy", ha="center",
            va="center", rotation=12, linespacing=1.1, zorder=8,
            path_effects=[patheffects.withStroke(linewidth=bold * 0.4, foreground=RED)] if bold else None)

    # 검은 띠: 단지명 · D-day
    ax.add_patch(Rectangle((0, 0), 1, 0.095, facecolor=INK, edgecolor="none"))
    write(0.05, 0.0475, name, 21, "white", limit=0.76)
    write(0.95, 0.0475, dday, 27, YELLOW, weight="heavy", family=heavy, ha="right", thick=bold)

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


if __name__ == "__main__":
    png = card_png(3, "맞벌이 신혼부부", "10월 12일 기준",
                   ["무주택 세대구성원", "혼인 7년 이내", "청약통장 6개월 이상"], "신청 가능",
                   "○○지구 신혼희망타운", "D-3", adults=2)
    assert png is None or png[:4] == b"\x89PNG"
    print("ok" if png else "ok (한글 글꼴 없음 → 썸네일 생략)")
