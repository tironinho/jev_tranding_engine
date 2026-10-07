from __future__ import annotations

import hashlib

# Do not edit this text in place. Add market_interpreter_v2 and point OPENAI_PROMPT_VERSION at it.
MARKET_INTERPRETER_V1 = """You are a market-state interpreter inside a quantitative crypto research platform.
You do not place orders.
You do not choose position size, leverage, stop, or target.
You do not change risk limits.
You do not give a buy or sell instruction.
You only describe the current market state from the feature snapshot in the user message.
Use only the numbers and flags provided. If a field is missing, do not invent it.
Evidence strings must name features that appear in the input.
Return only the structured object required by the schema.
"""

PROMPTS = {
    "market_interpreter_v1": MARKET_INTERPRETER_V1,
}

PROMPT_SHA256 = {
    version: hashlib.sha256(text.encode("utf-8")).hexdigest() for version, text in PROMPTS.items()
}


class UnknownPromptVersion(Exception):
    pass


def get_prompt(version: str) -> str:
    try:
        return PROMPTS[version]
    except KeyError as exc:
        raise UnknownPromptVersion(version) from exc


def prompt_sha256(version: str) -> str:
    get_prompt(version)
    return PROMPT_SHA256[version]
