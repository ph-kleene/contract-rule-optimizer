"""Validate the small, explicit input format before making model calls."""

from dataclasses import dataclass
from typing import Any


def required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    text: str


@dataclass(frozen=True)
class Contract:
    name: str
    text: str


def parse_rules(data: Any) -> list[Rule]:
    if not isinstance(data, list) or not data:
        raise ValueError("rules must be a non-empty JSON array")
    rules: list[Rule] = []
    ids: set[str] = set()
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("each rule must be a JSON object")
        rid = required_text(row.get("rule_id"), "rule_id")
        if rid in ids:
            raise ValueError("rule_id values must be unique")
        ids.add(rid)
        rules.append(Rule(rid, required_text(row.get("category"), "category"),
                          required_text(row.get("text"), "text")))
    return rules


def parse_contracts(data: Any) -> list[Contract]:
    if not isinstance(data, dict):
        raise ValueError("contracts must be an object mapping names to full text")
    return [Contract(required_text(name, "contract name"), required_text(text, "contract text"))
            for name, text in data.items()]
