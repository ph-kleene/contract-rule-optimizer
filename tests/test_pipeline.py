import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contract_rule_optimizer.cli import main
from contract_rule_optimizer.models import Contract, Rule, parse_contracts, parse_rules
from contract_rule_optimizer.pipeline import Optimizer, PipelineConfig
from contract_rule_optimizer.providers import ChatCompletionsModel, DemoModel, ModelError


class ScriptedModel:
    """Record prompts and provide stage-specific responses without network access."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def complete(self, stage, messages):
        self.calls.append((stage, copy.deepcopy(messages)))
        if not self.responses.get(stage):
            raise AssertionError(f"unexpected {stage} call")
        response = self.responses[stage].pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response, ensure_ascii=False) if isinstance(response, dict) else response


def candidate(statement):
    return {"rule_statement": statement, "parameters": {}}


def row(rid, statement, changed=False):
    return {"rule_id": rid, "statement": statement, "changed": changed, "change_reason": ""}


class PolishTests(unittest.TestCase):
    rule = Rule("R1", "示例", "原始规则")

    def test_rejection_feedback_then_acceptance(self):
        model = ScriptedModel(polish=[candidate("错误改写"), candidate("正确改写")],
                              judge=[{"accepted": False, "reason": "删除例外"}, {"accepted": True}])
        result = Optimizer(model).polish(self.rule)
        self.assertEqual(result["statement"], "正确改写")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertFalse(result["skipped"])
        second_polish = [messages for stage, messages in model.calls if stage == "polish"][1]
        self.assertIn("删除例外", second_polish[-1]["content"])
        self.assertIn("原始规则", second_polish[-1]["content"])

    def test_repeated_candidate_has_bounded_attempts_and_keeps_original(self):
        model = ScriptedModel(polish=[candidate("不合格") for _ in range(4)],
                              judge=[{"accepted": False, "reason": "改变立场"}])
        result = Optimizer(model).polish(self.rule)
        self.assertEqual(result["statement"], "原始规则")
        self.assertTrue(result["skipped"])
        self.assertEqual(len(result["attempts"]), 4)
        self.assertEqual(sum(stage == "judge" for stage, _ in model.calls), 1)

    def test_judge_missing_boolean_fails_closed(self):
        model = ScriptedModel(polish=[candidate("待审核")], judge=[{"reason": "没有判定"}])
        result = Optimizer(model).polish(self.rule)
        self.assertEqual(result["statement"], "原始规则")
        self.assertFalse(result["judge_accepted"])
        self.assertEqual(result["attempts"][0]["error"], "ValueError")

    def test_bad_output_and_provider_failure_keep_original_without_raw_error(self):
        for response in ("not-json-secret", candidate(""), ModelError("transport_error")):
            with self.subTest(response=type(response).__name__):
                model = ScriptedModel(polish=[response])
                result = Optimizer(model).polish(self.rule)
                self.assertEqual(result["statement"], "原始规则")
                self.assertTrue(result["skipped"])
                self.assertNotIn("not-json-secret", json.dumps(result))

    def test_unchanged_first_response_requires_no_judge(self):
        model = ScriptedModel(polish=[candidate("原始规则")])
        result = Optimizer(model).polish(self.rule)
        self.assertTrue(result["judge_accepted"])
        self.assertEqual(len(model.calls), 1)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.rules = [Rule("A", "示例", "规则A"), Rule("B", "示例", "规则B")]

    def model(self, validations):
        return ScriptedModel(polish=[candidate("规则A"), candidate("规则B")], validate=validations)

    def test_reordered_response_aligns_by_id_and_next_contract_sees_update(self):
        model = self.model([
            {"rules": [row("B", "规则B"), row("A", "规则A-更新", True)]},
            {"rules": [row("A", "规则A-更新"), row("B", "规则B-更新", True)]},
        ])
        result = Optimizer(model).run(self.rules, [Contract("一", "合同一"), Contract("二", "合同二")])
        self.assertEqual([r["final_statement"] for r in result["final_rules"]], ["规则A-更新", "规则B-更新"])
        second = [messages for stage, messages in model.calls if stage == "validate"][1]
        self.assertIn("规则A-更新", second[1]["content"])
        self.assertEqual(result["validate_layer"][0]["rules_before"]["A"], "规则A")
        self.assertEqual(result["validate_layer"][1]["rules_before"]["A"], "规则A-更新")

    def test_invalid_batch_is_atomic_and_preserves_all_rules(self):
        invalid_batches = [
            [row("A", "规则A-更新", True)],
            [row("A", "规则A-更新", True), row("A", "规则A-更新", True)],
            [row("A", "规则A-更新", True), row("X", "未知", True)],
            [row("A", "", True), row("B", "规则B")],
            [row("A", "规则A-更新", False), row("B", "规则B")],
            [row("A", "规则A", "true"), row("B", "规则B")],
        ]
        for rows in invalid_batches:
            with self.subTest(rows=rows):
                result = Optimizer(self.model([{"rules": rows}])).run(self.rules, [Contract("一", "正文")])
                self.assertEqual([r["final_statement"] for r in result["final_rules"]], ["规则A", "规则B"])
                self.assertEqual(result["validate_layer"][0]["error"], "ValueError")

    def test_failed_contract_does_not_prevent_later_contract(self):
        model = self.model([ModelError("transport_error"),
                            {"rules": [row("A", "规则A-更新", True), row("B", "规则B")]}])
        result = Optimizer(model).run(self.rules, [Contract("一", "正文一"), Contract("二", "正文二")])
        self.assertEqual(result["validate_layer"][0]["changes"], 0)
        self.assertEqual(result["final_rules"][0]["final_statement"], "规则A-更新")

    def test_character_limit_never_silently_truncates(self):
        model = self.model([])
        result = Optimizer(model, PipelineConfig(max_contract_chars=3)).run(self.rules, [Contract("一", "较长合同正文")])
        self.assertEqual(result["validate_layer"][0]["error"], "ValueError")
        self.assertFalse(any(stage == "validate" for stage, _ in model.calls))


class BoundaryTests(unittest.TestCase):
    def test_duplicate_inputs_and_empty_contract_text_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_rules([{"rule_id": "A", "category": "示例", "text": "一"},
                         {"rule_id": "A", "category": "示例", "text": "二"}])
        with self.assertRaises(ValueError):
            parse_contracts({"示例": ""})

    def test_remote_http_and_credentials_in_url_are_rejected(self):
        for url in ("http://example.com/v1", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ChatCompletionsModel(url, "example-test-key", "example-model")

    def test_demo_cli_is_offline_and_records_synthetic_change(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp, patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network called")):
            output = Path(temp) / "demo.json"
            status = main(["--mock", "--rules", str(root / "examples/rules.json"),
                           "--contracts", str(root / "examples/contracts.json"), "--output", str(output)])
            data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(data["mode"], "offline_mock")
        self.assertEqual(data["status"], "completed")
        self.assertIn("30至45日", data["final_rules"][0]["final_statement"])
        self.assertEqual([row["changes"] for row in data["validate_layer"]], [0, 1])


if __name__ == "__main__":
    unittest.main()
