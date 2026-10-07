"""설정 로딩.

사람마다 프로필이 하나씩 있다. 로컬·NAS 에서는 profiles/<별칭>.yaml 을 읽고,
GitHub Actions 에서는 PROFILES_YAML 시크릿 하나에 {별칭: 프로필} 로 넣은
YAML 문자열을 읽는다. 개인정보를 저장소에 커밋하지 않으면서 같은 코드가
어디서든 돌게 하기 위한 장치다.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "profiles"
DATA_DIR = ROOT / "data"
ENV_PATH = ROOT / ".env"


class MissingKey(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    """data/ 의 캐시·기록 파일. 없거나 깨졌으면 빈 dict 로 다시 시작한다."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_dotenv(path: Path = ENV_PATH) -> None:
    """프로젝트 루트의 .env 를 환경변수로 읽어 들인다.

    로컬에서 매번 키를 입력하지 않으려는 용도다. 이미 설정된 환경변수는
    덮어쓰지 않으므로, GitHub Actions 에서는 시크릿이 그대로 우선한다.
    """
    if not path.exists():
        return
    # 메모장이나 PowerShell 로 만든 파일에는 BOM 이 붙는다. utf-8-sig 가 이를 걷어낸다.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


load_dotenv()


# 별칭은 파일 이름·기록 파일 이름이 되므로 실명이 아닌 영문 ID 로 제한한다
ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def load_profiles() -> dict[str, dict]:
    """{별칭: 프로필}. PROFILES_YAML 이 있으면 그것만, 없으면 profiles/*.yaml (example 제외)."""
    raw = os.environ.get("PROFILES_YAML")
    if raw:
        profiles = yaml.safe_load(raw) or {}
    else:
        profiles = {
            path.stem: yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            for path in sorted(PROFILES_DIR.glob("*.yaml"))
            if path.stem != "example"
        }
    bad = [alias for alias in profiles if not ALIAS.fullmatch(str(alias))]
    if bad:
        raise MissingKey(f"별칭은 영문 소문자·숫자·-·_ 만 쓸 수 있습니다: {', '.join(map(str, bad))}")
    if not profiles:
        raise MissingKey("프로필이 없습니다. profiles/example.yaml 을 복사해 profiles/<별칭>.yaml 로 채워 주세요.")
    return profiles


def env(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise MissingKey(f"환경변수 {name} 가 설정되지 않았습니다.")
    return value


def parse_date(value) -> date | None:
    """YAML 에서 온 날짜를 date 로 정규화한다. None/빈 값은 그대로 None."""
    if value in (None, "", "TODO"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def months_between(start: date, end: date) -> int:
    """start 부터 end 까지 만 개월 수."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(months, 0)


if __name__ == "__main__":
    # profiles/*.yaml 을 합쳐 PROFILES_YAML 시크릿에 넣을 값을 파일로 만든다. 주석은 빠진다.
    # 한글 Windows 에서 클립보드로 바로 보내면 한글이 깨질 수 있어 파일로 쓴다.
    #   python -m src.config   →  preview/PROFILES_YAML.txt (gitignore 됨, 붙여 넣은 뒤 지울 것)
    os.environ.pop("PROFILES_YAML", None)
    # tester 는 로컬 시험용(--only tester)이라 Actions 로 보내지 않는다
    profiles = {alias: p for alias, p in load_profiles().items() if alias != "tester"}
    text = yaml.safe_dump(profiles, allow_unicode=True, sort_keys=False)
    out = ROOT / "preview" / "PROFILES_YAML.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    size = len(text.encode("utf-8"))
    # GitHub 시크릿 한 개 상한은 48KB(49,152바이트)
    print(f"{out} 에 저장 · {len(profiles)}명 · {size:,}바이트 / 상한 49,152바이트"
          + (" — 초과! 주석·null 항목을 줄이세요" if size > 49_152 else ""))
