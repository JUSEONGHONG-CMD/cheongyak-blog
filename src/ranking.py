"""공고 순위 매기기.

기본 정렬은 직장(최대 두 곳)까지의 대중교통 소요시간 합이다. 다만 마감이 코앞인 공고는
아무리 멀어도 먼저 보여야 하므로 상단에 따로 고정한다.
"""

from __future__ import annotations

from datetime import date

from .applyhome import apply_deadline
from .commute import UNKNOWN_MINUTES

URGENT_DAYS = 3


def days_left(item: dict, today: date) -> int | None:
    deadline = apply_deadline(item)
    return (deadline - today).days if deadline else None


def commute_scores(item: dict) -> tuple[int, int]:
    """(합계, 최악값). commute_total 이 없으면 commute 에서 직접 계산한다."""
    commute = item.get("commute") or {}
    if commute:
        values = list(commute.values())
        return sum(values), max(values)
    total = item.get("commute_total")
    if total is not None:
        return total, item.get("commute_worst", UNKNOWN_MINUTES)
    return UNKNOWN_MINUTES * 2, UNKNOWN_MINUTES


def sort_key(item: dict) -> tuple:
    """통근시간 합 → 최악값 → 마감 임박 순. 직장을 비운 프로필은 통근이 모두 같아 마감 순이 된다."""
    total, worst = commute_scores(item)
    left = item.get("days_left")
    return (total, worst, left if left is not None else 9999, item.get("name", ""))


def rank(items: list[dict], today: date | None = None) -> list[dict]:
    """마감 임박 건을 앞에 두고, 나머지는 통근시간 합 오름차순으로 정렬한다."""
    today = today or date.today()
    for item in items:
        item["days_left"] = days_left(item, today)

    for item in items:
        left = item["days_left"]
        item["urgent"] = left is not None and 0 <= left <= URGENT_DAYS

    urgent = [i for i in items if i["urgent"]]
    rest = [i for i in items if not i["urgent"]]

    urgent.sort(key=lambda i: (i["days_left"], sort_key(i)))
    rest.sort(key=sort_key)

    ordered = urgent + rest
    for index, item in enumerate(ordered, start=1):
        item["rank"] = index
    return ordered
