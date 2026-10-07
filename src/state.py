"""발송 이력. 사람마다 data/seen/<별칭>.json 하나씩.

알림에는 그날 신청 가능한 공고를 매번 전부 싣는다. 이력은 처음 보는 공고에
🆕 를 붙이는 데만 쓴다.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .config import DATA_DIR, load_json

SEEN_DIR = DATA_DIR / "seen"


class SeenStore:
    def __init__(self, alias: str, folder: Path = SEEN_DIR):
        self.path = folder / f"{alias}.json"
        self.data: dict[str, dict] = load_json(self.path)

    @staticmethod
    def key(item: dict) -> str:
        # LH 임대는 청약홈 번호가 없어 blog 가 seen_key 를 붙여 준다
        return item.get("seen_key") or f"{item.get('category')}:{item.get('house_manage_no')}:{item.get('pblanc_no')}"

    def is_new(self, item: dict) -> bool:
        return self.key(item) not in self.data

    def mark(self, item: dict, today: date) -> None:
        record = self.data.setdefault(
            self.key(item), {"first_sent": today.isoformat(), "name": item.get("name")}
        )
        record["last_sent"] = today.isoformat()

    def prune(self, today: date, keep_days: int = 180) -> None:
        """오래된 기록을 정리해 파일이 무한정 커지지 않게 한다."""
        cutoff = today.toordinal() - keep_days
        for key in list(self.data):
            last = self.data[key].get("last_sent") or self.data[key].get("first_sent")
            try:
                if date.fromisoformat(last).toordinal() < cutoff:
                    del self.data[key]
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
