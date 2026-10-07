from __future__ import annotations

import hashlib
import re


def normalize_hypothesis(strategy_family: str, text: str) -> str:
    compact = re.sub(r"\s+", " ", text.lower()).strip()
    compact = re.sub(r"[^a-z0-9 .+_<>=/-]", "", compact)
    return f"{strategy_family}|{compact}"


def hypothesis_fingerprint(strategy_family: str, text: str) -> str:
    return hashlib.sha256(normalize_hypothesis(strategy_family, text).encode("utf-8")).hexdigest()


def text_similarity(left: str, right: str) -> float:
    a = set(normalize_hypothesis("", left).split())
    b = set(normalize_hypothesis("", right).split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class ExperimentMemory:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def remember(self, record: dict) -> None:
        self.records.append(record)

    def find_similar(self, strategy_family: str, hypothesis: str, threshold: float = 0.8) -> dict | None:
        fingerprint = hypothesis_fingerprint(strategy_family, hypothesis)
        for record in self.records:
            if record.get("hypothesis_fingerprint") == fingerprint and record.get("strategy_family") == strategy_family:
                return record
            if record.get("strategy_family") != strategy_family:
                continue
            if text_similarity(record.get("hypothesis", ""), hypothesis) >= threshold:
                return record
        return None
