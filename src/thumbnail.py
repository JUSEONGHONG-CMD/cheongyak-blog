"""블로그 대표 이미지(썸네일) 카드.

네이버는 글의 첫 이미지를 검색 결과 썸네일로 쓴다. 한눈에 '누가 넣을 수 있는 청약인지,
언제 기준인지, 어디가 급한지, 어디 근처인지'가 보이게 한다.

    ┌──────────────────────────────┐
    │ 신생아 가구 신청 가능 청약    │  상단 띠
    ├──────────────────────────────┤
    │ 10월 12일 기준                │
    │ 새 청약 공고 3건              │  헤드라인
    │ 마감 임박 단지 : 더샵 분당센트로 │
    │ 임의공급 · D-1 마감           │
    │ 가까운 역 : 정자역 도보 9분    │  하단
    └──────────────────────────────┘

한글 글꼴이 없으면(GitHub 서버에 fonts-nanum 미설치 등) 네모로 깨지므로 아예 만들지 않는다.
"""

from __future__ import annotations

import io

from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

FONTS = ("NanumGothic", "Malgun Gothic", "AppleGothic", "Noto Sans CJK KR")
NAVY = "#1f3b5c"
ACCENT = "#f2b134"
LEFT, RIGHT = 0.08, 0.92  # 좌우 여백 8%. 글자가 이 폭을 넘으면 크기를 줄인다


def korean_font() -> str | None:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONTS if f in installed), None)


def card_png(band: str, date_line: str, headline: str, focus: str, sub: str, footer: str) -> bytes | None:
    """1080×1080 카드. 글꼴이 없으면 None."""
    font = korean_font()
    if not font:
        return None
    fig = Figure(figsize=(7.2, 7.2), dpi=150)
    FigureCanvasAgg(fig)  # 글자 폭을 재려면 실제 렌더러가 붙어 있어야 한다
    fig.patch.set_facecolor("white")
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    ax.add_patch(Rectangle((0, 0.76), 1, 0.24, facecolor=NAVY, edgecolor="none"))
    ax.add_patch(Rectangle((LEFT, 0.745), 0.18, 0.03, facecolor=ACCENT, edgecolor="none"))
    ax.add_patch(Rectangle((0, 0), 1, 0.13, facecolor="#f3f5f8", edgecolor="none"))

    def write(y, text, size, color, bold=False):
        t = ax.text(LEFT, y, text, color=color, fontsize=size, fontfamily=font, ha="left", va="center",
                    fontweight="bold" if bold else "normal")
        # 단지명·역 이름은 길이가 제각각이라, 폭을 넘으면 넘지 않을 때까지 줄인다
        renderer = fig.canvas.get_renderer()
        while size > 10 and t.get_window_extent(renderer).x1 > RIGHT * fig.bbox.width:
            size -= 1
            t.set_fontsize(size)

    write(0.88, band, 30, "white", bold=True)
    write(0.64, date_line, 20, "#666")
    write(0.55, headline, 36, NAVY, bold=True)
    write(0.40, focus, 22, "#222", bold=True)
    write(0.31, sub, 18, "#555")
    write(0.065, footer, 17, NAVY)
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


if __name__ == "__main__":
    png = card_png("신생아 가구 신청 가능 청약", "10월 12일 기준", "새 청약 공고 3건",
                   "마감 임박 단지 : 이천 중리지구 B3블록 금성백조 예미지(2차)", "임의공급 · D-1 마감",
                   "가까운 역 : 정자역 도보 9분")
    assert png is None or png[:4] == b"\x89PNG"
    print("ok" if png else "ok (한글 글꼴 없음 → 썸네일 생략)")
