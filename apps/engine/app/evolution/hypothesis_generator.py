from __future__ import annotations

VAGUE = {"melhore a estratégia", "improve the strategy", "make it better", "optimize the strategy"}


def accept_hypothesis(text: str) -> bool:
    cleaned = " ".join(text.lower().split())
    if cleaned in VAGUE or len(cleaned) < 24:
        return False
    return True


def hypotheses_from_report(recommended: list[str]) -> list[str]:
    return [item.strip() for item in recommended if accept_hypothesis(item)]
