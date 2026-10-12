"""Preserve the v5 method, with explicit ID alignment and conservative failures."""

import json
from dataclasses import dataclass
from typing import Any

from .models import Contract, Rule, required_text
from .prompts import (
    BATCH_VALIDATE_SYSTEM, BATCH_VALIDATE_USER, JUDGE_SYSTEM, JUDGE_USER,
    POLISH_SYSTEM, POLISH_USER,
)
from .providers import Model, ModelError, parse_object


@dataclass(frozen=True)
class PipelineConfig:
    max_retries: int = 3
    max_contract_chars: int = 0

    def __post_init__(self):
        if self.max_retries < 0 or self.max_contract_chars < 0:
            raise ValueError("limits must be non-negative")


def error_code(exc: Exception) -> str:
    # Raw responses, URLs, and provider errors may contain sensitive data.
    return str(exc) if isinstance(exc, ModelError) else type(exc).__name__


class Optimizer:
    def __init__(self, model: Model, config: PipelineConfig | None = None):
        self.model = model
        self.config = config or PipelineConfig()

    def polish(self, rule: Rule) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": POLISH_SYSTEM},
            {"role": "user", "content": POLISH_USER.format(rule_text=rule.text, category=rule.category)},
        ]
        attempts: list[dict[str, Any]] = []
        previous: str | None = None
        for number in range(1, self.config.max_retries + 2):
            try:
                raw = self.model.complete("polish", messages)
                result = parse_object(raw)
                statement = required_text(result.get("rule_statement"), "rule_statement")
                parameters = result.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ValueError("parameters must be an object")
                if number == 1 and statement.strip() == rule.text.strip():
                    attempts.append({"attempt": number, "accepted": True, "reason": "unchanged"})
                    return self._polish_result(statement, parameters, attempts, False)
                if number > 1 and statement.strip() == previous:
                    accepted, reason = False, "repeated_candidate"
                elif number > 1 and statement.strip() == rule.text.strip():
                    accepted, reason = False, "reverted_to_original"
                else:
                    judge_messages = [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": JUDGE_USER.format(input_rule=rule.text, output_rule=statement)},
                    ]
                    verdict = parse_object(self.model.complete("judge", judge_messages))
                    if type(verdict.get("accepted")) is not bool or not isinstance(verdict.get("reason", ""), str):
                        raise ValueError("judge must return accepted as a boolean and reason as a string")
                    accepted, reason = verdict["accepted"], verdict.get("reason", "")
                previous = statement.strip()
                attempts.append({"attempt": number, "candidate": statement,
                                 "accepted": accepted, "reason": reason})
                if accepted:
                    return self._polish_result(statement, parameters, attempts, False)
                messages.extend([
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"质量审核未通过：{reason}\n请根据反馈实质修改，保留原规则的立场、参数和例外，不要复读或回退。\n<待优化规则>\n{rule.text}\n</待优化规则>"},
                ])
            except (ModelError, ValueError, TypeError, KeyError) as exc:
                attempts.append({"attempt": number, "accepted": False, "error": error_code(exc)})
                # A malformed/failed response must never be treated as accepted.
                break
        return self._polish_result(rule.text, {}, attempts, True)

    @staticmethod
    def _polish_result(statement: str, parameters: dict, attempts: list, skipped: bool) -> dict:
        return {"statement": statement, "parameters": parameters, "attempts": attempts,
                "skipped": skipped, "judge_accepted": not skipped}

    def validate(self, current: list[dict], contract: Contract) -> tuple[list[dict], str]:
        if self.config.max_contract_chars and len(contract.text) > self.config.max_contract_chars:
            raise ValueError("contract exceeds configured limit; full text was not truncated")
        user = BATCH_VALIDATE_USER.format(
            rules_json=json.dumps(current, ensure_ascii=False, indent=2),
            contract_name=contract.name, contract_text=contract.text,
        )
        result = parse_object(self.model.complete("validate", [
            {"role": "system", "content": BATCH_VALIDATE_SYSTEM},
            {"role": "user", "content": user},
        ]))
        rows = result.get("rules")
        if not isinstance(rows, list):
            raise ValueError("validation response must contain a rules array")
        current_by_id = {row["rule_id"]: row for row in current}
        output: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("validation rows must be objects")
            rid = row.get("rule_id")
            if not isinstance(rid, str) or rid not in current_by_id or rid in output:
                raise ValueError("validation response has unknown or duplicate rule_id")
            statement = required_text(row.get("statement"), "statement")
            changed = row.get("changed")
            reason = row.get("change_reason", "")
            if type(changed) is not bool or not isinstance(reason, str):
                raise ValueError("invalid validation change metadata")
            actual_change = statement != current_by_id[rid]["current_statement"]
            if changed != actual_change:
                raise ValueError("changed flag disagrees with statement")
            output[rid] = {"rule_id": rid, "statement": statement,
                           "changed": changed, "change_reason": reason}
        if output.keys() != current_by_id.keys():
            raise ValueError("validation response must return every rule exactly once")
        summary = result.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("summary must be a string")
        return [output[row["rule_id"]] for row in current], summary

    def run(self, rules: list[Rule], contracts: list[Contract]) -> dict:
        if not rules or len({r.rule_id for r in rules}) != len(rules):
            raise ValueError("provide at least one rule with unique rule_id values")
        polished = [self.polish(rule) for rule in rules]
        current = [{"rule_id": rule.rule_id, "original_text": rule.text,
                    "current_statement": result["statement"]}
                   for rule, result in zip(rules, polished)]
        history = []
        for contract in contracts:
            before = {row["rule_id"]: row["current_statement"] for row in current}
            audit: dict[str, Any] = {"contract_name": contract.name, "rules_before": before.copy()}
            try:
                validated, summary = self.validate(current, contract)
                for row, updated in zip(current, validated):
                    row["current_statement"] = updated["statement"]
                audit.update({"validated": validated, "summary": summary,
                              "changes": sum(row["changed"] for row in validated)})
            except (ModelError, ValueError, TypeError, KeyError) as exc:
                audit.update({"error": error_code(exc), "changes": 0})
            audit["rules_after"] = {row["rule_id"]: row["current_statement"] for row in current}
            history.append(audit)
        return {
            "schema_version": "1.0",
            "polish_layer": [{"rule_id": rule.rule_id, **result}
                             for rule, result in zip(rules, polished)],
            "validate_layer": history,
            "final_rules": [{"rule_id": rule.rule_id, "category": rule.category,
                             "original_text": rule.text, "after_polish": polish["statement"],
                             "final_statement": state["current_statement"]}
                            for rule, polish, state in zip(rules, polished, current)],
        }
