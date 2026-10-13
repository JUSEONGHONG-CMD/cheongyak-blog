"""블로그 대표 이미지(썸네일) 카드.

네이버는 글의 첫 이미지를 검색 결과 썸네일로 쓴다. 사용자가 고른 시안(2026-10-12)을 따른다.

    ┌──────────────────────────────────┐
    │ (인물) 모델 03          10월 12일 기준 │  파란 띠
    │        맞벌이 신혼부부                 │
    ├──────────────────────────────────┤
    │ 의왕역 한신더휴                       │
    │ 지원할 수 있어요                      │  큰 제목 (단지명 + 판정)
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


# 인물 아이콘: woman·man 은 어른, girl·boy 는 아이. 옷 색과 머리 모양으로 성별을 구분한다
SHIRTS = {"woman": "#ff7aa2", "man": "#1f3b5c", "girl": "#ffb3c7", "boy": "#5ec98f"}


def _person(ax, x: float, kind: str, hair: str, z: int) -> float:
    """한 사람을 그리고 다음 사람의 x 를 돌려준다."""
    small = kind in ("girl", "boy")
    female = kind in ("woman", "girl")
    r = 0.022 if small else 0.03
    base, body_h = 0.835, (0.045 if small else 0.06)
    cy = base + body_h + r * 0.9
    if kind == "woman":  # 어깨까지 오는 긴 머리: 얼굴 뒤에 깐다
        ax.add_patch(FancyBboxPatch((x - r * 1.15, cy - r * 1.35), r * 2.3, r * 2.3,
                                    boxstyle=f"round,pad=0,rounding_size={r}", facecolor=hair,
                                    edgecolor="none", zorder=z))
    if kind == "girl":  # 양 갈래 머리
        for side in (-1, 1):
            ax.add_patch(Circle((x + side * r * 1.15, cy + r * 0.1), r * 0.45, facecolor=hair, zorder=z))
    ax.add_patch(FancyBboxPatch((x - r * 1.25, base), r * 2.5, body_h, boxstyle="round,pad=0,rounding_size=0.02",
                                facecolor=SHIRTS[kind], edgecolor="none", zorder=z + 1))
    if kind == "man":  # 넥타이
        ax.add_patch(Rectangle((x - r * 0.12, base + body_h * 0.25), r * 0.24, body_h * 0.7,
                               facecolor="#e8434b", edgecolor="none", zorder=z + 2))
    ax.add_patch(Circle((x, cy), r, facecolor=SKIN, edgecolor="none", zorder=z + 3))
    # 앞머리: 여자는 둥근 앞머리, 남자는 짧게 위만
    ax.add_patch(Wedge((x, cy + r * (0.2 if female else 0.4)), r * 1.02, 0 if female else 15,
                       180 if female else 165, facecolor=hair, edgecolor="none", zorder=z + 4))
    for side in (-1, 1):  # 눈
        ax.add_patch(Circle((x + side * r * 0.38, cy - r * 0.15), r * 0.11, facecolor=INK, zorder=z + 5))
        if female:  # 볼 터치
            ax.add_patch(Circle((x + side * r * 0.62, cy - r * 0.45), r * 0.16, facecolor="#ff9fb0",
                                zorder=z + 5))
    ax.add_patch(Wedge((x, cy - r * 0.38), r * 0.28, 200, 340, width=r * 0.08, facecolor="#c0504d",
                       edgecolor="none", zorder=z + 5))  # 입
    return x + (0.042 if small else 0.05)


def _people(ax, icons: list[str], senior: bool) -> float:
    """띠 왼쪽 가족 아이콘. 그린 뒤 오른쪽 끝 x 를 돌려준다."""
    hair = "#a7a7a7" if senior else HAIR
    x = 0.065
    for i, kind in enumerate(icons):
        x = _person(ax, x, kind, hair, 3 + i * 10)
    return x


def short_name(name: str, limit: int = 16) -> str:
    """카드 큰 제목용 단지명. 길면 띄어쓰기 단위로 앞쪽만 남긴다(단어 중간에서 자르지 않음)."""
    if len(name) <= limit:
        return name
    kept = ""
    for word in name.split():
        if len(f"{kept} {word}".strip()) > limit:
            break
        kept = f"{kept} {word}".strip()
    return kept or name[: limit - 1] + "…"


def card_png(model_no: int, label: str, date_line: str, checks: list[str], stamp: str,
             name: str, dday: str, icons: list[str] = ("man",), senior: bool = False,
             verb: str = "지원할 수 있어요", facts: str = "") -> bytes | None:
    """1080×1080 카드. 글꼴이 없으면 None.

    name 은 큰 제목 첫 줄(단지명), verb 는 둘째 줄, facts 는 검은 띠(유형·평수·분양가)."""
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
    left = _people(ax, list(icons), senior) + 0.01
    write(left, 0.945, f"모델 {model_no:02d}", 14, BLUE_SOFT, weight="normal")
    write(left, 0.885, label, 25, "white", limit=0.72)
    write(0.95, 0.945, date_line, 13, BLUE_SOFT, weight="normal", ha="right")

    # 큰 제목
    bold = 0 if heavy != font else 3
    # 단지명은 길이가 제각각이라 폭에 맞춰 줄인다(write 가 처리). 너무 길면 띄어쓰기 단위로 앞쪽만
    title = short_name(name)
    write(0.05, 0.705, title, 54, INK, weight="heavy", family=heavy, thick=bold, limit=0.93)
    write(0.05, 0.565, verb, 54, INK, weight="heavy", family=heavy, thick=bold)

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
    write(0.05, 0.0475, facts or name, 21, "white", limit=0.76)
    write(0.95, 0.0475, dday, 27, YELLOW, weight="heavy", family=heavy, ha="right", thick=bold)

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


if __name__ == "__main__":
    png = card_png(3, "맞벌이 신혼부부", "10월 12일 기준",
                   ["무주택 세대구성원", "혼인 7년 이내", "청약통장 6개월 이상"], "신청 가능",
                   "의왕역 한신더휴", "D-6", icons=["woman", "man", "girl", "boy"],
                   facts="무순위 · 전용 84㎡(26평) · 9.4억")
    assert png is None or png[:4] == b"\x89PNG"
    print("ok" if png else "ok (한글 글꼴 없음 → 썸네일 생략)")
