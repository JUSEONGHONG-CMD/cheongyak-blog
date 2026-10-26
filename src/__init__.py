"""패키지를 불러오는 순간 출력에서 API 키를 가린다.

공개 저장소의 Actions 로그는 누구나 보고, 조회 실패 메시지에는 키가 붙은 요청 주소가 통째로 찍힌다.
예전에는 main() 에서만 켜서, 점검 스크립트처럼 모듈을 직접 부르면 키가 그대로 찍혔다(2026-10-26).
그래서 어떤 모듈을 먼저 불러도 걸리도록 여기에 둔다.
"""

import re
import sys


class Redacted:
    """출력에서 serviceKey= 뒤 값을 지운다. GitHub 시크릿 가림에만 기대지 않으려고 출력 단계에서 한 번 더."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        return self.stream.write(re.sub(r"(serviceKey=)[^&\s'\"]+", r"\1***", text))

    def __getattr__(self, name):
        return getattr(self.stream, name)


if not isinstance(sys.stdout, Redacted):
    sys.stdout, sys.stderr = Redacted(sys.stdout), Redacted(sys.stderr)
