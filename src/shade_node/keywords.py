"""Shared keyword catalog used by query-driven collectors."""
from pathlib import Path
import tomllib
import re

TIERS = ("local", "regional", "national", "global")

def normalize_keywords(payload: dict) -> dict[str, list[str]]:
    result = {}
    sections = payload.get("keywords", {})
    for tier in TIERS:
        block = sections.get(tier, {})
        terms = block.get("terms", []) if isinstance(block, dict) else []
        if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
            raise ValueError(f"Invalid keywords.{tier}.terms")
        result[tier] = list(dict.fromkeys(term.strip() for term in terms if term.strip()))
    return result

def load_keywords(path: str | Path) -> dict[str, list[str]]:
    with Path(path).open("rb") as handle:
        return normalize_keywords(tomllib.load(handle))

def tag_safe(term: str) -> str:
    return re.sub(r"[^\w]", "", term, flags=re.UNICODE)

def hashtags(keywords: dict[str, list[str]]) -> list[str]:
    seen = set(); result = []
    for tier in ("local", "regional"):
        for term in keywords.get(tier, []):
            tag = tag_safe(term)
            if tag and tag.lower() not in seen:
                seen.add(tag.lower()); result.append(tag)
    return result
