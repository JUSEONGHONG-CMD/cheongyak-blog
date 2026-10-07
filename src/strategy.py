"""어느 방법으로 신청하는 게 유리한지 추천한다.

판정이 "신청할 수 있다"를 알려준다면, 여기서는 "그중 무엇부터 넣을지"를 고른다.
제도상 확실한 규칙만 근거로 삼고, 당첨 가능성 같은 추정은 하지 않는다.

근거로 쓰는 규칙은 세 가지다.
  - 같은 단지에 특별공급 1건과 일반공급 1건을 중복 신청할 수 있다. 특별공급에
    당첨되면 일반공급 선정에서 빠지므로, 둘 다 넣는 쪽이 항상 낫다.
  - 물량이 적은 특별공급 유형일수록 경쟁이 심하다.
  - 가점이 낮으면 가점제 물량은 사실상 어려우므로 추첨 물량을 노려야 한다.
"""

from __future__ import annotations

# 이 점수보다 낮으면 수도권 인기 단지의 가점제 물량은 기대하기 어렵다.
LOW_GAPJEOM = 40


def _units(item: dict, label: str) -> int:
    return int((item.get("special_counts") or {}).get(label) or 0)


# 청약통장을 쓰지 않아 당첨돼도 통장이 남는 유형들
NO_ACCOUNT_TYPES = ("오피스텔등", "임의공급", "공공지원민간임대", "무순위")


def recommend(item: dict, verdict) -> list[str]:
    """추천 문장 목록.

    선택지가 하나뿐이어도 빈 목록을 돌려주지 않는다. 메시지의 항목 번호는
    공고마다 같은 뜻이어야 하는데, 어떤 날은 ⑨가 추천이고 어떤 날은 참고가
    되면 훑어보기 어려워진다.
    """
    possible = verdict.possible
    if not possible:
        return ["자격이 확실치 않습니다. 모집공고문에서 직접 확인하세요."]

    special = [r for r in possible if "특별공급" in r.name]
    general = [r for r in possible if "일반공급 추첨제" in r.name]
    unranked = [r for r in possible if "무순위" in r.name]

    lines: list[str] = []

    if unranked:
        lines.append("청약통장을 쓰지 않고 자격도 느슨합니다. 먼저 넣어보세요.")
        lines.append("  당첨돼도 통장이 남아 다음 기회를 그대로 노릴 수 있습니다.")
        return lines

    if not special and not general:
        # 오피스텔·임의공급·공공지원민간임대처럼 경로가 하나뿐인 유형
        lines.append(f"선택지는 '{possible[0].name}' 하나입니다.")
        if item.get("category") in NO_ACCOUNT_TYPES:
            lines.append("  청약통장을 쓰지 않아 당첨돼도 통장이 남습니다.")
            lines.append("  아파트 청약에 영향이 없으니 부담 없이 넣어볼 만합니다.")
        return lines

    if special:
        # 같은 유형의 여러 구간 중에서는 소득 기준이 느슨한 쪽(추첨분)이 덜 빡빡하다.
        # 다만 물량은 추첨분이 가장 적으므로, 유형 선택은 물량 기준으로 한다.
        best = max(special, key=lambda r: _units(item, _label_of(r.name)))
        units = _units(item, _label_of(best.name))
        unit_text = f" (물량 {units}세대)" if units else ""
        lines.append(f"①순위: {best.name}{unit_text}")

        others = [r for r in special if r is not best]
        if others:
            lines.append(
                "  특별공급은 한 단지에 한 유형만 넣을 수 있어, 물량이 많은 쪽을 골랐습니다."
            )

    if general:
        order = "②순위" if special else "①순위"
        lines.append(f"{order}: 일반공급 추첨제")
        if special:
            lines.append(
                "  특별공급과 일반공급은 같은 단지에 중복 신청됩니다. 둘 다 넣으세요."
            )
            lines.append("  특공에 당첨되면 일반공급은 자동으로 제외되니 손해가 없습니다.")

    if general and verdict.gapjeom < LOW_GAPJEOM:
        lines.append(
            f"  가점 {verdict.gapjeom}점으로는 가점제 물량이 어렵습니다. 추첨 물량만 노리세요."
        )

    return lines


def couple_advice(verdict) -> list[str]:
    """혼인신고로 신청 건수를 늘릴 수 있는지 알린다.

    공고마다 반복하면 잔소리가 되므로 요약 메시지에서 하루 한 번만 쓴다.

    2024년 3월부터 부부 중복청약이 허용돼, 혼인신고한 부부는 같은 단지에
    각자 신청할 수 있고 둘 다 당첨돼도 먼저 접수한 건이 유효하다. 신청 건수가
    곧 확률이므로, 두 사람 모두 1순위라면 혼인신고 시점이 곧 전략이 된다.
    """
    accounts = getattr(verdict, "accounts", None) or []
    if len(accounts) < 2:
        return []

    both_ready = all(route.status == "가능" for _, route in accounts)
    if not both_ready:
        blocked = [label for label, route in accounts if route.status != "가능"]
        return [f"※ {', '.join(blocked)} 통장 조건이 걸려 부부 중복청약은 아직 어렵습니다."]

    if "혼인신고 완료" in (verdict.marriage_basis or ""):
        return ["※ 부부가 각자 신청하면 기회가 2배입니다. 둘 다 넣으세요."]
    return [
        "※ 혼인신고하면 부부가 각자 넣어 기회가 2배가 됩니다.",
        "   두 사람 모두 1순위라 지금은 그 점이 가장 큰 차이를 만듭니다.",
    ]


def _label_of(route_name: str) -> str:
    """경로 이름에서 특별공급 유형 이름을 뽑아 물량 조회 키로 쓴다."""
    if "신혼부부" in route_name:
        return "신혼부부"
    if "생애최초" in route_name:
        return "생애최초"
    if "신생아" in route_name:
        return "신생아"
    return ""
