"""CLI entry point. No network calls unless the caller omits --mock."""

import argparse
import json
import sys
from pathlib import Path

from .models import parse_contracts, parse_rules
from .pipeline import Optimizer, PipelineConfig
from .providers import ChatCompletionsModel, DemoModel


def read_json(path: Path):
    # utf-8-sig also accepts files exported with a UTF-8 BOM on Windows.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polish review rules, then validate them against full contracts.")
    parser.add_argument("--rules", type=Path, required=True, help="JSON rule array")
    parser.add_argument("--contracts", type=Path, required=True, help="JSON object: contract name -> full text")
    parser.add_argument("--output", type=Path, default=Path("output/result.json"))
    parser.add_argument("--mock", action="store_true", help="synthetic offline simulation; makes no model calls")
    parser.add_argument("--max-retries", type=int, default=3, help="Judge rejection retries (default: 3)")
    parser.add_argument("--max-contract-chars", type=int, default=0,
                        help="reject oversized contracts without truncation; 0 = no limit")
    args = parser.parse_args(argv)
    try:
        rules = parse_rules(read_json(args.rules))
        contracts = parse_contracts(read_json(args.contracts))
        config = PipelineConfig(args.max_retries, args.max_contract_chars)
        model = DemoModel() if args.mock else ChatCompletionsModel.from_environment()
        result = Optimizer(model, config).run(rules, contracts)
        result["mode"] = "offline_mock" if args.mock else "live_model"
        failed = sum(row["skipped"] for row in result["polish_layer"]) + sum(
            "error" in row for row in result["validate_layer"])
        result["status"] = "completed_with_errors" if failed else "completed"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{result['status']}: {len(rules)} rules, {len(contracts)} contracts; {args.output}")
        if args.mock:
            print("Offline fixture only: output does not measure model or legal quality.")
        return 2 if failed else 0
    except (OSError, ValueError) as exc:
        # Deliberately omit raw JSON and provider configuration from error output.
        print(f"Input/configuration error ({type(exc).__name__}). Check JSON files, limits, and environment settings.", file=sys.stderr)
        return 1
